"""Non-renewable runtime leases; no source, threads, subprocesses or local timers.

The injected native driver is an ADDITIONAL trusted component, outside the Python
supervisor. It must durably arm an exact-container cutoff in the platform before
start, surviving both supervisor and authority-process death. No such backend is
implemented/provisioned here: the default driver refuses every operation.
"""
import math
import re
import sqlite3
import json
import threading
from contextlib import contextmanager
from uuid import uuid4
from typing import Literal
from app.execution.python_models import StrictModel
from pydantic import Field, ConfigDict, model_validator


class DeadlineUnavailable(ValueError): pass


class LeaseTarget(StrictModel):
    job_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    plan_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    container_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    runtime_identity: str = Field(min_length=1, max_length=200)
    config_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    case_index: int = Field(ge=0, le=9)


class LeaseReceipt(LeaseTarget):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    authority_id: str
    lease_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    boot_id: str = Field(min_length=1, max_length=200)
    armed_at: float = Field(ge=0)
    expires_at: float = Field(ge=0)
    job_expires_at: float = Field(ge=0)
    state: Literal['arming','armed','starting','running','stopping','terminated','uncertain']
    termination_confirmed: bool
    start_confirmed: bool
    receipt_version: Literal[2] | None = None
    attempt_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    registered_at: float | None = Field(default=None, ge=0)
    timing: dict | None = None
    deadline_met: bool | None = None

    @model_validator(mode='after')
    def bounded_cutoff(self):
        if (self.authority_id != self.runtime_identity + ':deadlines-v1'
                or not self.armed_at < self.expires_at <= min(self.armed_at + 5, self.job_expires_at)
                or not self.armed_at < self.job_expires_at <= self.armed_at + 30
                or self.termination_confirmed != (self.state == 'terminated')):
            raise ValueError('Invalid deadline binding')
        proven = None
        if self.timing is not None:
            observed = TerminationTiming.model_validate(self.timing)
            if (any(getattr(observed, key) != value for key, value in self.model_dump(include=set(LeaseTarget.model_fields)).items())
                    or observed.lease_id != self.lease_id or observed.attempt_id != self.attempt_id
                    or observed.boot_id != self.boot_id or not self.termination_confirmed):
                raise ValueError('Termination timing binding mismatch')
            if observed.workload_stopped_at is not None:
                if self.registered_at is None or observed.workload_stopped_at < self.registered_at:
                    raise ValueError('Cessation predates registration')
                proven = observed.workload_stopped_at <= self.expires_at
        if self.deadline_met is not proven:
            raise ValueError('Deadline claim is not supported by native cessation evidence')
        return self


class TerminationTiming(LeaseTarget):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    lease_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    attempt_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    boot_id: str = Field(min_length=1, max_length=200)
    source: Literal['native-runtime-observer']
    cutoff_requested_at: float | None = Field(ge=0)
    termination_confirmed_at: float = Field(ge=0)
    workload_stopped_at: float | None = Field(ge=0)
    reason: Literal['completed','deadline','cancelled','recovered']

    @model_validator(mode='after')
    def ordered(self):
        if ((self.workload_stopped_at is not None and self.workload_stopped_at > self.termination_confirmed_at)
                or (self.cutoff_requested_at is not None and self.cutoff_requested_at > self.termination_confirmed_at)
                or (self.reason != 'completed' and self.cutoff_requested_at is None)
                or (self.reason != 'completed' and self.workload_stopped_at is not None
                    and self.cutoff_requested_at > self.workload_stopped_at)):
            raise ValueError('Inconsistent native timing observations')
        return self


def checked_receipt(raw, target, state, started=False, lease_id=None, attempt_id=None):
    receipt = LeaseReceipt.model_validate(raw)
    if (any(getattr(receipt, key) != value for key, value in target.model_dump().items())
            or receipt.state != state or (started and not receipt.start_confirmed)
            or receipt.receipt_version != 2 or receipt.attempt_id is None
            or (lease_id is not None and receipt.lease_id != lease_id)
            or (attempt_id is not None and receipt.attempt_id != attempt_id)
            or receipt.registered_at is None or not receipt.armed_at <= receipt.registered_at < receipt.expires_at):
        raise DeadlineUnavailable('Native cutoff receipt mismatch')
    return receipt.model_dump()


class UnavailableNativeDeadlineDriver:
    """A Timer, manifest boolean or successful Docker inspect is not a native cutoff."""
    def __getattr__(self, name):
        raise DeadlineUnavailable('Crash-independent native deadline driver is unavailable')


class DeadlineAuthority:
    """Fenced state-machine core, NOT a native cutoff implementation.

    Native arm/start/terminate take an immutable attempt ID. terminate must fence
    delayed starts for that attempt before acknowledging cancellation. No SQLite
    protocol can replace that native obligation; live activation remains blocked.
    """
    def __init__(self, path, runtime_identity, authorize, driver=None):
        self.path, self.runtime_identity, self.authorize = path, runtime_identity, authorize
        self.driver = driver if driver is not None else UnavailableNativeDeadlineDriver()
        self.lock = threading.RLock()  # Convenience only; durable SQL checks fence peers.
        self.instance, self.epoch, self.reconciled = uuid4().hex, None, False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS deadline_leases (
                lease TEXT PRIMARY KEY, job TEXT NOT NULL, digest TEXT NOT NULL,
                cid TEXT NOT NULL UNIQUE, runtime TEXT NOT NULL, config TEXT NOT NULL,
                case_index INTEGER NOT NULL, boot TEXT NOT NULL, armed REAL NOT NULL,
                expires REAL NOT NULL, job_expires REAL NOT NULL, last_clock REAL NOT NULL,
                state TEXT NOT NULL, started INTEGER NOT NULL DEFAULT 0, UNIQUE(job,case_index))''')
            # Additive private-journal migration. Preserve all old rows/tombstones;
            # absent historical timing remains unknown, never synthesized as proof.
            columns = {r['name'] for r in db.execute('PRAGMA table_info(deadline_leases)')}
            for name, sql_type in [('generation','INTEGER NOT NULL DEFAULT 0'), ('operation','TEXT'),
                                   ('attempt','TEXT'), ('registered','REAL'), ('timing','TEXT')]:
                if name not in columns: db.execute(f'ALTER TABLE deadline_leases ADD COLUMN {name} {sql_type}')
            db.execute('CREATE TABLE IF NOT EXISTS deadline_quarantine(id INTEGER PRIMARY KEY CHECK(id=1))')
            db.execute('''CREATE TABLE IF NOT EXISTS deadline_authority (
                id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL, owner TEXT, ready INTEGER NOT NULL)''')
            db.execute('INSERT OR IGNORE INTO deadline_authority VALUES(1,0,NULL,0)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def _owned(self, db, ready=True):
        row = db.execute('SELECT * FROM deadline_authority WHERE id=1').fetchone()
        if (row['owner'] != self.instance or row['epoch'] != self.epoch
                or (ready and (not self.reconciled or not row['ready']))):
            raise DeadlineUnavailable('Current instance must reconcile; authority generation changed')

    def _ready(self):
        if not self.reconciled: raise DeadlineUnavailable('Native restart reconciliation is required')
        with self.db() as db:
            self._owned(db)
            if db.execute('SELECT 1 FROM deadline_quarantine').fetchone(): raise DeadlineUnavailable('Native cutoff quarantined')

    def _clock(self):
        boot, stamp = self.driver.boot_id(), self.driver.clock()
        if not isinstance(boot, str) or not boot or not math.isfinite(stamp) or stamp < 0:
            raise DeadlineUnavailable('Invalid native clock')
        with self.db() as db:
            last = db.execute('SELECT max(last_clock) FROM deadline_leases WHERE boot=?', (boot,)).fetchone()[0]
        if last is not None and stamp < last: raise DeadlineUnavailable('Native clock rolled backwards')
        return boot, stamp

    def quarantine(self):
        self.reconciled = False
        with self.db() as db:
            # A stale worker cannot quarantine or clear a replacement authority.
            row = db.execute('SELECT * FROM deadline_authority WHERE id=1').fetchone()
            if row['owner'] == self.instance and row['epoch'] == self.epoch:
                db.execute('INSERT OR IGNORE INTO deadline_quarantine VALUES(1)')
                db.execute('UPDATE deadline_authority SET ready=0 WHERE id=1 AND owner=? AND epoch=?', (self.instance,self.epoch))

    @staticmethod
    def target(row):
        return LeaseTarget(job_id=row['job'], plan_digest=row['digest'], container_id=row['cid'],
                           runtime_identity=row['runtime'], config_sha256=row['config'], case_index=row['case_index'])

    def _row(self, lease):
        with self.db() as db: row = db.execute('SELECT * FROM deadline_leases WHERE lease=?', (lease,)).fetchone()
        if row is None: raise DeadlineUnavailable('Unknown native lease')
        return row

    def _commit(self, lease, operation, expected, assignments, values, ready=True):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE'); self._owned(db,ready)
            result = db.execute(f'''UPDATE deadline_leases SET {assignments}
                WHERE lease=? AND operation=? AND generation=? AND state=?''',
                (*values,lease,operation,self.epoch,expected))
            if result.rowcount != 1: raise DeadlineUnavailable('Operation superseded; acknowledgement discarded')

    def receipt(self, lease):
        row = self._row(lease)
        timing = json.loads(row['timing']) if row['timing'] else None
        stopped = timing.get('workload_stopped_at') if timing else None
        return dict(self.target(row).model_dump(), authority_id=self.runtime_identity+':deadlines-v1',
            lease_id=row['lease'], boot_id=row['boot'], armed_at=row['armed'], expires_at=row['expires'],
            job_expires_at=row['job_expires'], state=row['state'], termination_confirmed=row['state']=='terminated',
            start_confirmed=bool(row['started']), receipt_version=2 if row['attempt'] else None,
            attempt_id=row['attempt'], registered_at=row['registered'], timing=timing,
            deadline_met=None if stopped is None else stopped <= row['expires'])

    def arm(self, target):
        target = LeaseTarget.model_validate(target.model_dump())
        self._ready()
        if target.runtime_identity != self.runtime_identity: raise DeadlineUnavailable('Foreign runtime')
        lease, operation, attempt = uuid4().hex, uuid4().hex, uuid4().hex
        try:
            boot, stamp = self._clock(); self.authorize(target)
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE'); self._owned(db)
                if db.execute('SELECT 1 FROM deadline_quarantine').fetchone(): raise DeadlineUnavailable('Cleanup uncertain')
                if db.execute("SELECT 1 FROM deadline_leases WHERE state!='terminated'").fetchone(): raise DeadlineUnavailable('An existing lease must be cleaned first')
                prior = db.execute('SELECT * FROM deadline_leases WHERE job=? ORDER BY armed LIMIT 1',(target.job_id,)).fetchone()
                job_expires = stamp+30
                if prior:
                    if prior['boot']!=boot or prior['digest']!=target.plan_digest: raise DeadlineUnavailable('Job clock/approval changed')
                    job_expires = prior['job_expires']
                expires = min(stamp+5,job_expires)
                if stamp>=expires: raise DeadlineUnavailable('Job deadline expired')
                db.execute('''INSERT INTO deadline_leases
                    (lease,job,digest,cid,runtime,config,case_index,boot,armed,expires,job_expires,last_clock,
                     state,started,generation,operation,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'arming',0,?,?,?)''',
                    (lease,target.job_id,target.plan_digest,target.container_id,target.runtime_identity,target.config_sha256,
                     target.case_index,boot,stamp,expires,job_expires,stamp,self.epoch,operation,attempt))
            ack = self.driver.arm(target,lease,boot,expires,attempt)
            registered = ack.get('registered_at')
            expected = dict(target.model_dump(),lease_id=lease,boot_id=boot,expires_at=expires,
                            attempt_id=attempt,registered_at=registered)
            if (ack!=expected or type(registered) not in {int,float} or not math.isfinite(registered)
                    or not stamp<=registered<expires): raise DeadlineUnavailable('Native registration acknowledgement mismatch')
            current_boot,current=self._clock()
            if current_boot!=boot or not registered<=current<expires:
                raise DeadlineUnavailable('Native registration acknowledgement is stale or in the future')
            self._commit(lease,operation,'arming',"state='armed',registered=?,last_clock=?",(registered,current))
            return self.receipt(lease)
        except BaseException:
            try: current=self._row(lease)
            except DeadlineUnavailable: current=None
            if (current is not None and current['operation']==operation
                    and current['generation']==self.epoch and current['state']=='arming'):
                self.quarantine()
            raise

    def start(self, lease):
        # Reject a reopened instance BEFORE driver/cleanup calls or ledger changes.
        self._ready()
        row = self._row(lease)
        if row['state']!='armed': raise DeadlineUnavailable('Start cannot be repeated')
        operation, claimed = uuid4().hex, False
        try:
            boot, stamp = self._clock()
            if boot!=row['boot'] or not row['last_clock']<=stamp<row['expires']: raise DeadlineUnavailable('Lease expired or clock changed')
            target=self.target(row); self.authorize(target)
            if self.driver.observe(target) is not True: raise DeadlineUnavailable('Container binding changed')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE'); self._owned(db)
                result=db.execute("""UPDATE deadline_leases SET state='starting',operation=?,last_clock=?
                    WHERE lease=? AND state='armed' AND operation=? AND generation=? AND attempt=?""",
                    (operation,stamp,lease,row['operation'],self.epoch,row['attempt']))
                if result.rowcount!=1: raise DeadlineUnavailable('Start ownership changed; no replay')
            claimed=True
            # The native provider must fence this immutable attempt against
            # cancellation/expiry even when this RPC is delayed in transit.
            ack=self.driver.start(target,lease,row['attempt'])
            if ack!={'lease_id':lease,'container_id':target.container_id,'attempt_id':row['attempt'],'started':True}:
                raise DeadlineUnavailable('Native start acknowledgement uncertain')
            current_boot,current=self._clock()
            if current_boot!=boot or not stamp<=current<row['expires']: raise DeadlineUnavailable('Start acknowledgement arrived after cutoff')
            self._commit(lease,operation,'starting',"state='running',started=1,last_clock=?",(current,))
            return self.receipt(lease)
        except BaseException:
            # A late/losing acknowledgement must not alter newer cancellation,
            # reconciliation or completion state (including its timing evidence).
            current=self._row(lease)
            if current['state']=='starting' and current['operation']==operation and current['generation']==self.epoch:
                self.quarantine()
                try: self.finish(lease)
                except Exception: pass
            elif not claimed and current['state']=='armed':
                self.quarantine()
                try: self.finish(lease)
                except Exception: pass
            raise

    def finish(self, lease):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE'); self._owned(db,False)
            row=db.execute('SELECT * FROM deadline_leases WHERE lease=?',(lease,)).fetchone()
            if row is None: raise DeadlineUnavailable('Unknown native lease')
            if row['state']=='terminated': return self.receipt(lease)
            if row['state']=='stopping' and row['generation']==self.epoch:
                raise DeadlineUnavailable('Cleanup already in progress')
            operation=uuid4().hex; attempt=row['attempt'] or uuid4().hex
            # Cancellation intent is durable BEFORE native I/O. It fences every
            # outstanding arm/start acknowledgement, across authority instances.
            claimed=db.execute("""UPDATE deadline_leases SET state='stopping',operation=?,generation=?,attempt=?
                WHERE lease=? AND state=? AND operation IS ? AND generation=?""",
                (operation,self.epoch,attempt,lease,row['state'],row['operation'],row['generation']))
            if claimed.rowcount!=1: raise DeadlineUnavailable('Cleanup operation superseded')
        target=self.target(row)
        try:
            self.driver.terminate(target,lease,attempt)
            if self.driver.absent(target) is not True: raise DeadlineUnavailable('Termination unconfirmed')
            observation=self.driver.termination_evidence(target,lease,attempt)
            timing=None
            if observation is not None:
                timing=TerminationTiming.model_validate(observation)
                if (any(getattr(timing,k)!=v for k,v in target.model_dump().items()) or timing.lease_id!=lease
                        or timing.attempt_id!=attempt or timing.boot_id!=row['boot']
                        or (timing.workload_stopped_at is not None and (row['registered'] is None
                            or timing.workload_stopped_at<row['registered']))):
                    raise DeadlineUnavailable('Native termination timing binding mismatch')
            if self.driver.disarm(target,lease) is not True: raise DeadlineUnavailable('Native cutoff cleanup unconfirmed')
            try:
                boot,stamp=self._clock()
                latest=max(row['last_clock'],stamp) if boot==row['boot'] else row['last_clock']
            except Exception:
                latest=row['last_clock']; self.quarantine()
            self._commit(lease,operation,'stopping',"state='terminated',last_clock=?,timing=?",
                         (latest,timing.model_dump_json() if timing else None),False)
            return self.receipt(lease)
        except BaseException as exc:
            try:
                self._commit(lease,operation,'stopping',"state='uncertain'",(),False)
                self.quarantine()
            except DeadlineUnavailable: pass  # A newer operation owns the outcome.
            if isinstance(exc,ValueError) and not isinstance(exc,DeadlineUnavailable):
                raise DeadlineUnavailable('Invalid native termination timing evidence') from None
            raise

    def stop_job(self, job_id):
        if not re.fullmatch('[a-f0-9]{32}',job_id): raise DeadlineUnavailable('Invalid job')
        with self.db() as db:
            self._owned(db,False)
            rows=db.execute("SELECT lease FROM deadline_leases WHERE job=? AND state!='terminated'",(job_id,)).fetchall()
        for row in rows: self.finish(row['lease'])
        self._orphans(job_id)
        return True

    def _orphans(self, job_id=None):
        with self.db() as db: self._owned(db,False)
        for raw in self.driver.inventory():
            target=LeaseTarget.model_validate(raw)
            if target.runtime_identity!=self.runtime_identity: raise DeadlineUnavailable('Foreign inventory entry')
            if job_id is not None and target.job_id!=job_id: continue
            self.driver.terminate(target,None,None)
            if self.driver.absent(target) is not True or self.driver.release_orphan(target) is not True:
                self.quarantine(); raise DeadlineUnavailable('Orphan cleanup unconfirmed')
        if any(job_id is None or raw['job_id']==job_id for raw in self.driver.inventory()):
            self.quarantine(); raise DeadlineUnavailable('Managed inventory remains')

    def tick(self):
        with self.db() as db: self._owned(db,False)
        try:
            boot,stamp=self._clock()
            with self.db() as db: rows=db.execute("SELECT * FROM deadline_leases WHERE state!='terminated'").fetchall()
            for row in rows:
                if row['state']=='stopping': continue
                if boot!=row['boot'] or stamp<row['last_clock'] or stamp>=row['expires']:
                    self.finish(row['lease']); continue
                try: self.authorize(self.target(row))
                except Exception: self.finish(row['lease']); continue
                self._commit(row['lease'],row['operation'],row['state'],'last_clock=?',(stamp,),False)
        except BaseException:
            self.quarantine()
            with self.db() as db: rows=db.execute("SELECT lease FROM deadline_leases WHERE state NOT IN ('terminated','stopping')").fetchall()
            for row in rows:
                try: self.finish(row['lease'])
                except Exception: pass
            raise

    def reconcile(self):
        # Taking ownership fences every older instance and in-flight ack. A new
        # instance may only clean previous work, never resume an armed lease.
        self.reconciled=False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE deadline_authority SET epoch=epoch+1,owner=?,ready=0 WHERE id=1',(self.instance,))
            self.epoch=db.execute('SELECT epoch FROM deadline_authority WHERE id=1').fetchone()[0]
        try:
            clock_valid=True
            try: self._clock()
            except Exception: clock_valid=False
            with self.db() as db: rows=db.execute("SELECT lease FROM deadline_leases WHERE state!='terminated'").fetchall()
            for row in rows: self.finish(row['lease'])
            self._orphans()
            if not clock_valid: raise DeadlineUnavailable('Native clock untrusted; cleanup attempted')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE'); self._owned(db,False)
                if db.execute("SELECT 1 FROM deadline_leases WHERE state!='terminated'").fetchone():
                    raise DeadlineUnavailable('Cleanup incomplete')
                db.execute('DELETE FROM deadline_quarantine')
                db.execute('UPDATE deadline_authority SET ready=1 WHERE id=1 AND owner=? AND epoch=?',(self.instance,self.epoch))
            self.reconciled=True
        except BaseException:
            self.quarantine(); raise

class DeadlineClient:
    """Client contract only. No native authority service is provisioned/started here."""
    def __init__(self, path):
        from types import SimpleNamespace
        from app.execution.sandbox import SupervisorClient
        self.transport = SupervisorClient(SimpleNamespace(sandbox_supervisor_socket=path))

    def arm(self, target): return self.transport.request('POST', '/leases', target.model_dump())
    def start(self, lease): return self.transport.request('POST', '/start/' + lease)
    def finish(self, lease): return self.transport.request('POST', '/finish/' + lease)
    def receipt(self, lease): return self.transport.request('GET', '/leases/' + lease)
    def stop_job(self, job): return self.transport.request('POST', '/stop/' + job) == {'cleanup_confirmed': True}
