"""Safety-contract simulations only. No containers, clocks/services provisioned,
candidate evaluation, approvals, live sockets or workspace tools are used.
The fake native backend models an external timer; it does not prove one exists.
"""
from unittest.mock import MagicMock
from uuid import uuid4
import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
pytest.importorskip('fcntl', reason='POSIX supervisor contracts require fcntl; NOT TESTED on Windows')
from app.execution.deadlines import DeadlineAuthority, DeadlineUnavailable, LeaseTarget, LeaseReceipt, checked_receipt
from app.execution.docker_supervisor import DockerRuntime, DockerAPI, DockerUnavailable
from app.execution.python_models import canonical, verify_python
from app.execution.sandbox import SupervisorClient, config_hash
from app.execution.workspace import Workspace
from app.execution.agents import WorkspaceExecutor
from test_python_sandbox import settings, plan_for, evidence


@pytest.fixture(autouse=True)
def no_live_tools(monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail('Live operation forbidden')
    for cls, method in [(DockerAPI,'request'),(DockerAPI,'attach'),(SupervisorClient,'request'),
                        (Workspace,'read'),(Workspace,'create'),(WorkspaceExecutor,'execute')]:
        monkeypatch.setattr(cls, method, forbidden)


def target(job=None, index=0, cid='a'*64):
    return LeaseTarget(job_id=job or uuid4().hex, plan_digest='b'*64, container_id=cid,
        runtime_identity='mock-vm', config_sha256='c'*64, case_index=index)


class FakeNative:
    """Timer state lives outside the simulated authority/supervisor objects."""
    def __init__(self):
        self.stamp=0.0; self.boot='boot-A'; self.containers={}; self.tickets={}
        self.running=set(); self.starts=[]; self.killed=[]; self.events=[]
        self.bad_ack=False; self.cleanup_fails=False; self.disarm_fails=False
        self.lost_start_ack=False
        self.cancelled=set(); self.stop_times={}; self.timing_missing=False
    def boot_id(self): return self.boot
    def clock(self): return self.stamp
    def add(self, t): self.containers[t.container_id]=t.model_copy(deep=True)
    def advance(self, seconds):
        self.stamp += seconds
        # Simulates PLATFORM-owned cutoffs without calling authority.tick().
        for t, lease, boot, expires, attempt in list(self.tickets.values()):
            if self.boot != boot or self.stamp >= expires:
                self.running.discard(t.container_id); self.containers.pop(t.container_id,None)
                self.killed.append(t.container_id)
                self.stop_times[t.container_id]=expires
    def arm(self, t, lease, boot, expires, attempt):
        self.events.append('native-arm')
        assert (lease,attempt) not in self.cancelled
        self.tickets[lease]=(t,lease,boot,expires,attempt)
        ack=dict(t.model_dump(),lease_id=lease,boot_id=boot,expires_at=expires,
                 attempt_id=attempt,registered_at=self.stamp)
        if self.bad_ack: ack['container_id']='f'*64
        return ack
    def observe(self,t): return self.containers.get(t.container_id)==t
    def start(self,t,lease,attempt):
        self.events.append('native-start')
        if lease not in self.tickets or (lease,attempt) in self.cancelled:
            raise DeadlineUnavailable('Simulated native cancellation fence rejected start')
        assert lease in self.tickets and self.tickets[lease][0]==t
        assert self.boot==self.tickets[lease][2] and self.stamp<self.tickets[lease][3]
        assert self.tickets[lease][4]==attempt and (lease,attempt) not in self.cancelled
        self.running.add(t.container_id); self.starts.append(t.container_id)
        if self.lost_start_ack: raise RuntimeError('Acknowledgement lost after start')
        return dict(lease_id=lease,container_id=t.container_id,attempt_id=attempt,started=True)
    def terminate(self,t,lease,attempt):
        self.events.append('terminate')
        if self.cleanup_fails: raise RuntimeError('Daemon unavailable')
        self.cancelled.add((lease,attempt))
        self.running.discard(t.container_id); self.containers.pop(t.container_id,None)
        self.killed.append(t.container_id)
        self.stop_times.setdefault(t.container_id,self.stamp)
    def termination_evidence(self,t,lease,attempt):
        if self.timing_missing or t.container_id not in self.starts or lease not in self.tickets:
            return None
        ticket=self.tickets[lease]
        if self.boot!=ticket[2]: return None
        stopped=self.stop_times[t.container_id]
        return dict(t.model_dump(),lease_id=lease,attempt_id=attempt,boot_id=self.boot,
            source='native-runtime-observer',cutoff_requested_at=stopped,
            workload_stopped_at=stopped,termination_confirmed_at=self.stamp,reason='cancelled')
    def absent(self,t):
        self.events.append('confirm-absent')
        return t.container_id not in self.containers
    def disarm(self,t,lease):
        self.events.append('disarm')
        assert t.container_id not in self.containers
        if self.disarm_fails: return False
        self.tickets.pop(lease,None); return True
    def inventory(self): return [t.model_dump() for t in self.containers.values()]
    def release_orphan(self,t):
        assert t.container_id not in self.containers
        if self.disarm_fails: return False
        for lease,(item,*_) in list(self.tickets.items()):
            if item.container_id==t.container_id: self.tickets.pop(lease)
        return True


@pytest.fixture
def core(tmp_path):
    native=FakeNative(); authorize=MagicMock()
    authority=DeadlineAuthority(tmp_path/'leases.sqlite3','mock-vm',authorize,native)
    authority.reconcile()
    return authority,native,authorize


def armed(core,t=None):
    authority,native,_=core; t=t or target(); native.add(t)
    receipt=authority.arm(t)
    return t,receipt


def test_native_component_absent_fails_closed(tmp_path):
    authority=DeadlineAuthority(tmp_path/'leases.sqlite3','mock-vm',MagicMock())
    with pytest.raises(DeadlineUnavailable): authority.reconcile()
    with pytest.raises(DeadlineUnavailable): authority.arm(target())


def test_supervisor_and_authority_death_do_not_cancel_simulated_native_cutoff(core):
    authority,native,_=core
    t,receipt=armed(core)
    authority.start(receipt['lease_id'])
    assert native.events.index('native-arm') < native.events.index('native-start')
    # No authority/supervisor methods or Python Timer callbacks after this point.
    del authority
    native.advance(5)
    assert t.container_id not in native.running and t.container_id not in native.containers


def test_ack_loss_after_start_is_cleanup_only_not_replay(core):
    authority,native,_=core
    t,r=armed(core); native.lost_start_ack=True
    with pytest.raises(RuntimeError): authority.start(r['lease_id'])
    assert native.starts==[t.container_id] and t.container_id not in native.running
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    assert len(native.starts)==1


def test_duplicate_arm_never_extends_deadline(core):
    authority,native,_=core
    t,r=armed(core); native.advance(1)
    with pytest.raises(Exception): authority.arm(t)
    assert authority.receipt(r['lease_id'])['expires_at']==5.0
    assert len(native.tickets)==1


def test_restart_reconciles_exact_containers_and_never_starts(core):
    old,native,authorize=core
    t,r=armed(core); old.start(r['lease_id'])
    replacement=DeadlineAuthority(old.path,'mock-vm',authorize,native)
    with pytest.raises(DeadlineUnavailable): replacement.arm(target(cid='d'*64))
    replacement.reconcile()
    assert replacement.receipt(r['lease_id'])['termination_confirmed']
    assert native.starts==[t.container_id]


@pytest.mark.parametrize('change',['boot','rollback','expired'])
def test_changed_clocks_block_start_and_terminate_exact_target(core,change):
    authority,native,_=core
    t,r=armed(core)
    if change=='boot': native.boot='boot-B'
    elif change=='rollback': native.stamp=-1.0
    else: native.advance(5)
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    assert native.starts==[] and t.container_id not in native.containers


def test_total_job_deadline_is_never_reset_by_a_new_case(core):
    authority,native,_=core
    t,r=armed(core); authority.start(r['lease_id']); authority.finish(r['lease_id'])
    native.advance(28)
    other=target(job=t.job_id,index=1,cid='d'*64); native.add(other)
    second=authority.arm(other)
    assert second['expires_at']==second['job_expires_at']==30.0
    authority.start(second['lease_id']); native.advance(2)
    assert other.container_id not in native.running


def test_termination_confirmed_before_native_cutoff_is_disarmed(core):
    authority,native,_=core
    t,r=armed(core); authority.start(r['lease_id'])
    stopped=authority.finish(r['lease_id'])
    assert stopped['termination_confirmed'] and stopped['start_confirmed']
    assert native.events.index('terminate') < native.events.index('confirm-absent') < native.events.index('disarm')


@pytest.mark.parametrize('failure',['termination','disarm','ack'])
def test_native_failures_quarantine_new_jobs(core,failure):
    authority,native,_=core
    if failure=='ack':
        native.bad_ack=True
        with pytest.raises(DeadlineUnavailable): armed(core)
    else:
        t,r=armed(core); authority.start(r['lease_id'])
        if failure=='termination': native.cleanup_fails=True
        else: native.disarm_fails=True
        with pytest.raises(Exception): authority.finish(r['lease_id'])
    with pytest.raises(DeadlineUnavailable): authority.arm(target(cid='e'*64))
    assert len(native.starts)<=1


def test_job_stop_never_terminates_a_different_job(core):
    authority,native,_=core
    t,r=armed(core); authority.start(r['lease_id'])
    authority.stop_job(uuid4().hex)
    assert t.container_id in native.running
    authority.stop_job(t.job_id)
    assert t.container_id not in native.running


def test_create_before_registration_crash_is_orphan_cleanup_only(core):
    authority,native,_=core
    orphan=target(); native.add(orphan)
    authority.reconcile()
    assert orphan.container_id not in native.containers and native.starts==[]


def test_foreign_runtime_is_never_deleted_on_recovery(core):
    authority,native,_=core
    foreign=target().model_copy(update={'runtime_identity':'another-vm'})
    native.add(foreign)
    with pytest.raises(DeadlineUnavailable): authority.reconcile()
    assert native.killed==[] and foreign.container_id in native.containers


def test_authorization_loss_stops_instead_of_replaying(core):
    authority,native,authorize=core
    t,r=armed(core); authority.start(r['lease_id'])
    authorize.side_effect=ValueError('Ownership lost')
    authority.tick()
    assert t.container_id not in native.running and native.starts==[t.container_id]


def test_native_arming_does_not_hold_sqlite_write_transaction(core):
    authority,native,_=core
    original=native.arm
    def inspect_locks(*args):
        with authority.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('SELECT count(*) FROM deadline_leases')
        return original(*args)
    native.arm=inspect_locks
    armed(core)


def test_clock_failure_still_attempts_exact_container_cleanup(core):
    authority,native,_=core
    t,r=armed(core); authority.start(r['lease_id'])
    native.stamp=-1.0
    with pytest.raises(DeadlineUnavailable): authority.tick()
    assert t.container_id not in native.running
    with pytest.raises(DeadlineUnavailable): authority.reconcile()
    with pytest.raises(DeadlineUnavailable): authority.arm(target(cid='f'*64))


def test_rollback_after_a_completed_case_cannot_extend_job_time(core):
    authority,native,_=core
    t,r=armed(core); authority.start(r['lease_id']); native.advance(4)
    authority.finish(r['lease_id']); native.stamp=1.0
    second=target(job=t.job_id,index=1,cid='e'*64); native.add(second)
    with pytest.raises(DeadlineUnavailable): authority.arm(second)
    assert native.starts==[t.container_id]


def test_no_deadline_authority_blocks_creation_and_live_preflight(settings):
    adapter=DockerRuntime(plan_for(settings).steps[0].tool.runtime,MagicMock())
    with pytest.raises(DockerUnavailable,match='crash-independent'): adapter.preflight()
    with pytest.raises(DockerUnavailable,match='before container creation'):
        adapter.case(None,0,'a'*32,None,None,None,0,None,0,'b'*64)
    adapter.api.request.assert_not_called()


@pytest.mark.parametrize('field',['container_id','plan_digest','runtime_identity','config_sha256','job_id','expires_at','start_confirmed','termination_confirmed'])
def test_forged_native_receipt_cannot_verify_python(settings,field):
    call=plan_for(settings).steps[0].tool
    actual=evidence(call,uuid4().hex,'a'*64)
    cutoffs=[dict(c.deadline_receipt) for c in actual.cases]
    if field in {'start_confirmed','termination_confirmed'}: cutoffs[0][field]=False
    elif field=='expires_at': cutoffs[0][field]=1000.0
    else: cutoffs[0][field]='wrong'
    passed,_=verify_python(call,actual,actual.model_copy(deep=True),actual.job_id,'a'*64,config_hash(call.runtime),cutoffs)
    assert not passed


def test_reopened_armed_journal_cannot_start_without_reconciliation(core):
    authority,native,authorize=core
    t,r=armed(core)
    reopened=DeadlineAuthority(authority.path,'mock-vm',authorize,native)
    before=list(native.events)
    with pytest.raises(DeadlineUnavailable,match='reconciliation'):
        reopened.start(r['lease_id'])
    assert native.starts==[] and native.events==before
    assert reopened.receipt(r['lease_id'])['state']=='armed'
    reopened.reconcile()
    assert reopened.receipt(r['lease_id'])['state']=='terminated'
    with pytest.raises(DeadlineUnavailable): reopened.start(r['lease_id'])
    assert native.starts==[]


def test_cancel_between_observe_and_start_claim_prevents_native_start(core):
    authority,native,_=core; t,r=armed(core)
    observe=native.observe
    def cancel_before_claim(item):
        result=observe(item)
        authority.finish(r['lease_id'])
        return result
    native.observe=cancel_before_claim
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    assert native.starts==[] and authority.receipt(r['lease_id'])['state']=='terminated'


def test_delayed_start_ack_cannot_overwrite_cancellation(core):
    authority,native,_=core; t,r=armed(core)
    start=native.start
    def late(item,lease,attempt):
        ack=start(item,lease,attempt)
        authority.finish(lease)
        return ack
    native.start=late
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    final=authority.receipt(r['lease_id'])
    assert final['state']=='terminated' and not final['start_confirmed']
    assert native.starts==[t.container_id] and t.container_id not in native.running


def test_delayed_start_ack_cannot_overwrite_recovery_generation(core):
    authority,native,authorize=core; t,r=armed(core)
    replacement=DeadlineAuthority(authority.path,'mock-vm',authorize,native)
    start=native.start
    def late(item,lease,attempt):
        ack=start(item,lease,attempt)
        replacement.reconcile()
        return ack
    native.start=late
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    assert replacement.receipt(r['lease_id'])['state']=='terminated'
    assert replacement.reconciled
    with replacement.db() as db: assert db.execute('SELECT ready FROM deadline_authority').fetchone()[0]==1
    assert len(native.starts)==1


def test_delayed_arm_ack_cannot_resurrect_recovered_lease(core):
    authority,native,authorize=core
    replacement=DeadlineAuthority(authority.path,'mock-vm',authorize,native)
    arm=native.arm
    def late(*args):
        ack=arm(*args); replacement.reconcile(); return ack
    native.arm=late
    with pytest.raises(DeadlineUnavailable): armed(core)
    with replacement.db() as db:
        assert db.execute('SELECT state FROM deadline_leases').fetchone()[0]=='terminated'
        assert db.execute('SELECT ready FROM deadline_authority').fetchone()[0]==1
    assert native.starts==[]


def test_delayed_cleanup_ack_cannot_overwrite_newer_recovery_receipt(core):
    authority,native,authorize=core; t,r=armed(core); authority.start(r['lease_id'])
    replacement=DeadlineAuthority(authority.path,'mock-vm',authorize,native)
    terminate=native.terminate; first=True; newer={}
    def late(*args):
        nonlocal first
        terminate(*args)
        if first:
            first=False; replacement.reconcile()
            newer.update(replacement.receipt(r['lease_id']))
    native.terminate=late
    with pytest.raises(DeadlineUnavailable): authority.finish(r['lease_id'])
    assert replacement.receipt(r['lease_id'])==newer
    assert replacement.reconciled and native.starts==[t.container_id]


def test_delayed_start_ack_after_expiry_is_not_success(core):
    authority,native,_=core; t,r=armed(core); start=native.start
    def late(*args):
        ack=start(*args); native.advance(6); return ack
    native.start=late
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    final=authority.receipt(r['lease_id'])
    assert final['state']=='terminated' and not final['start_confirmed']
    assert len(native.starts)==1


def test_duplicate_start_does_not_start_or_cancel_the_winner(core):
    authority,native,_=core; t,r=armed(core); authority.start(r['lease_id'])
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    assert native.starts==[t.container_id] and t.container_id in native.running


def test_independent_connections_and_locks_use_sql_start_claim(core):
    authority,native,_=core; t,r=armed(core)
    # Simulate two workers in one authorized generation, with independent locks
    # and SQLite connections. This is not a real multi-process/native test.
    peer=copy.copy(authority); peer.lock=threading.RLock()
    entered,release=threading.Event(),threading.Event()
    original=native.observe; first=True
    def observe(item):
        nonlocal first
        result=original(item)
        if first:
            first=False; entered.set(); assert release.wait(5)
        return result
    native.observe=observe
    with ThreadPoolExecutor() as pool:
        pending=pool.submit(authority.start,r['lease_id'])
        assert entered.wait(5)
        try: assert peer.start(r['lease_id'])['state']=='running'
        finally: release.set()
        with pytest.raises(DeadlineUnavailable): pending.result()
    assert native.starts==[t.container_id]
    assert authority.receipt(r['lease_id'])['state']=='running'


def test_missing_native_cessation_time_is_unknown_not_a_deadline_claim(core):
    authority,native,_=core; t,r=armed(core); authority.start(r['lease_id'])
    native.timing_missing=True
    receipt=authority.finish(r['lease_id'])
    assert receipt['termination_confirmed']
    assert receipt['deadline_met'] is None and receipt['timing'] is None


def test_legacy_journal_is_migrated_additively_without_inventing_timing(tmp_path):
    path=tmp_path/'old.sqlite3'; t=target(); lease=uuid4().hex
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE deadline_leases (
            lease TEXT PRIMARY KEY,job TEXT,digest TEXT,cid TEXT UNIQUE,runtime TEXT,config TEXT,
            case_index INTEGER,boot TEXT,armed REAL,expires REAL,job_expires REAL,last_clock REAL,
            state TEXT,started INTEGER,UNIQUE(job,case_index))''')
        db.execute('INSERT INTO deadline_leases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (lease,t.job_id,t.plan_digest,t.container_id,t.runtime_identity,t.config_sha256,0,'boot-A',0,5,30,0,'terminated',1))
    authority=DeadlineAuthority(path,'mock-vm',MagicMock(),FakeNative())
    receipt=authority.receipt(lease)
    assert receipt['start_confirmed'] and receipt['termination_confirmed']
    assert receipt['receipt_version'] is None and receipt['deadline_met'] is None
    assert receipt['registered_at'] is None and receipt['timing'] is None
    LeaseReceipt.model_validate(receipt)


@pytest.mark.parametrize('change',['missing','unknown','late','false_claim','wrong_attempt','wrong_boot',
    'wrong_source','inconsistent','old_receipt','wrong_lease'])
def test_timing_evidence_cannot_be_missing_fabricated_or_stale(settings,change):
    call=plan_for(settings).steps[0].tool
    actual=evidence(call,uuid4().hex,'a'*64)
    raw=actual.cases[0].deadline_receipt
    if change=='missing': raw['timing']=None; raw['deadline_met']=None
    elif change=='unknown': raw['timing']['workload_stopped_at']=None; raw['deadline_met']=None
    elif change in {'late','false_claim'}:
        raw['timing']['workload_stopped_at']=6.0; raw['timing']['termination_confirmed_at']=7.0
        raw['deadline_met']=change=='false_claim'
    elif change=='wrong_attempt': raw['timing']['attempt_id']='e'*32
    elif change=='wrong_boot': raw['timing']['boot_id']='stale-boot'
    elif change=='wrong_source': raw['timing']['source']='candidate-stdout'
    elif change=='inconsistent': raw['timing']['cutoff_requested_at']=10.0
    elif change=='old_receipt':
        for key in ['receipt_version','attempt_id','registered_at','timing','deadline_met']: raw.pop(key)
    else: raw['timing']['lease_id']='f'*32
    cutoffs=[copy.deepcopy(c.deadline_receipt) for c in actual.cases]
    passed,_=verify_python(call,actual,actual.model_copy(deep=True),actual.job_id,'a'*64,config_hash(call.runtime),cutoffs)
    assert not passed


def test_late_confirmation_does_not_imply_late_workload_cessation(settings):
    call=plan_for(settings).steps[0].tool
    actual=evidence(call,uuid4().hex,'a'*64)
    receipt=actual.cases[0].deadline_receipt
    receipt['timing']['termination_confirmed_at']=6.0
    verified=LeaseReceipt.model_validate(receipt)
    assert verified.deadline_met is True  # Supported only by the fake trusted stop timestamp.


@pytest.mark.parametrize('mode',['expired','future'])
def test_registration_acknowledgement_must_be_current(core,mode):
    authority,native,_=core; original=native.arm
    def delayed(*args):
        ack=original(*args)
        if mode=='expired': native.advance(6)
        else: ack['registered_at']=4.0
        return ack
    native.arm=delayed
    with pytest.raises(DeadlineUnavailable): armed(core)
    assert native.starts==[]


@pytest.mark.parametrize('field',['lease_id','attempt_id'])
def test_incorrect_start_acknowledgement_identity_is_cleanup_only(core,field):
    authority,native,_=core; t,r=armed(core); original=native.start
    def wrong(*args):
        ack=original(*args); ack[field]='f'*32; return ack
    native.start=wrong
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    receipt=authority.receipt(r['lease_id'])
    assert receipt['state']=='terminated' and not receipt['start_confirmed']
    assert native.starts==[t.container_id]


def test_cancelled_attempt_cannot_be_started_by_a_delayed_native_request(core):
    authority,native,_=core; t,r=armed(core); original=native.start
    def delayed(item,lease,attempt):
        authority.finish(lease)
        return original(item,lease,attempt)
    native.start=delayed
    with pytest.raises(DeadlineUnavailable): authority.start(r['lease_id'])
    assert native.starts==[] and authority.receipt(r['lease_id'])['state']=='terminated'


def test_late_cleanup_failure_cannot_mark_replacement_receipt_uncertain(core):
    authority,native,authorize=core; t,r=armed(core); authority.start(r['lease_id'])
    replacement=DeadlineAuthority(authority.path,'mock-vm',authorize,native)
    original=native.terminate; first=True; newer={}
    def delayed(*args):
        nonlocal first
        original(*args)
        if first:
            first=False; replacement.reconcile(); newer.update(replacement.receipt(r['lease_id']))
            raise RuntimeError('Stale cleanup failure')
    native.terminate=delayed
    with pytest.raises(RuntimeError): authority.finish(r['lease_id'])
    assert replacement.receipt(r['lease_id'])==newer and replacement.reconciled
