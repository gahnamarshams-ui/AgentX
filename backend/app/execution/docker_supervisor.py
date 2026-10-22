"""Separately operated supervisor. Never started automatically by AgentX.

Uses Docker's local Unix API, not a shell/CLI/subprocess. Candidate code is only
transmitted to the approved image over attached stdin. Nothing is pulled/built.
No claim of isolation is made until separately authorized live validation.
"""
import hashlib
import http.client
import json
import re
import socket
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode
from app.execution.python_models import PythonCall, PythonRuntime, PythonEvidence, CaseEvidence, canonical, fingerprint
from app.execution.sandbox import ENV, SECCOMP, UnixHTTP, container_spec, config_hash
from app.execution.deadlines import LeaseTarget, checked_receipt


def now(): return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class DockerUnavailable(Exception): pass


class DockerAPI:
    """Fixed local socket; no DOCKER_HOST, contexts, credential helpers or env inheritance."""
    def __init__(self, socket_path): self.socket_path = socket_path

    def request(self, method, path, body=None):
        conn = UnixHTTP(self.socket_path, 2)
        try:
            conn.request(method, '/v1.45' + path, body=canonical(body) if body is not None else None,
                         headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            data = response.read(1048577)
            if response.status not in {200, 201, 204} or len(data) > 1048576:
                raise DockerUnavailable('Docker request failed')
            return json.loads(data) if data else None
        except (OSError, ValueError, http.client.HTTPException):
            raise DockerUnavailable('Docker unavailable') from None
        finally: conn.close()

    def attach(self, container_id):
        # HTTP hijack: keep framed stdout/stderr separate and bound raw byte counts.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(str(self.socket_path))
            path = f'/v1.45/containers/{container_id}/attach?stream=1&stdin=1&stdout=1&stderr=1'
            sock.sendall(f'POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n'.encode())
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                part = sock.recv(1)
                if not part or len(header) > 16384: raise DockerUnavailable('Invalid attach handshake')
                header.extend(part)
            if not bytes(header).startswith(b'HTTP/1.1 101 '):
                raise DockerUnavailable('Docker attach upgrade unavailable')
            sock.settimeout(0.1)
            return sock
        except BaseException:
            sock.close()
            raise


class DockerRuntime:
    def __init__(self, runtime: PythonRuntime, api: DockerAPI, deadlines=None):
        self.runtime, self.api = runtime, api
        self.deadlines = deadlines

    def preflight(self):
        # A service-local Timer survives backend failure, not supervisor failure.
        # Neither configuration nor either feature flag may override this release
        # block. A crash-independent deadline needs separate design/live review.
        raise DockerUnavailable('Live activation blocked: crash-independent deadline enforcement is not established')

    def inspect_environment(self):
        """Metadata checks only, never authorization to create/start containers."""
        r = self.runtime
        if r.seccomp_sha256 != fingerprint(SECCOMP): raise DockerUnavailable('Profile mismatch')
        info = self.api.request('GET', '/info')
        version = self.api.request('GET', '/version')
        identity = f"docker:{info['ID']}:{version['Version']}:{version['KernelVersion']}:linux/arm64"
        if (r.identity != identity or info.get('OSType') != 'linux'
                or info.get('Architecture') not in {'aarch64', 'arm64'}
                or 'Docker Desktop' not in info.get('OperatingSystem', '')
                or not all(info.get(flag) is True for flag in ['MemoryLimit', 'SwapLimit', 'PidsLimit', 'CpuCfsQuota'])
                or not any('seccomp' in option for option in info.get('SecurityOptions', []))):
            raise DockerUnavailable('Runtime identity or isolation features unavailable')
        image = self.api.request('GET', '/images/' + r.image + '/json')
        config = image.get('Config', {})
        if (image.get('Id') != r.image or image.get('Architecture') != 'arm64'
                or image.get('Os') != 'linux' or config.get('Volumes')
                or config.get('Env') != ENV
                or config.get('Labels', {}).get('agentx.bootstrap.sha256') != r.bootstrap_sha256):
            raise DockerUnavailable('Untrusted runner image')

    def inspect_config(self, cid, job_id):
        actual = self.api.request('GET', '/containers/' + cid + '/json')
        expected = container_spec(self.runtime)
        config, host = actual['Config'], actual['HostConfig']
        for key, value in expected.items():
            if key == 'HostConfig': continue
            if config.get(key) != value: raise DockerUnavailable('Container configuration mismatch')
        for key, value in expected['HostConfig'].items():
            # Docker omits optional empty arrays or normalizes restart metadata.
            observed = host.get(key)
            if value == [] and observed is None: observed = []
            if key == 'RestartPolicy': observed = {'Name': (observed or {}).get('Name')}
            if observed != value: raise DockerUnavailable('Container isolation mismatch')
        if (actual.get('Image') != self.runtime.image
                or config.get('Labels', {}).get('agentx.job') != job_id
                or host.get('PortBindings') or host.get('ExtraHosts') or host.get('Links')
                or host.get('DeviceCgroupRules') or host.get('Sysctls')
                or host.get('UsernsMode') not in {None, '', 'private'}
                or config.get('Volumes')
                or any(m.get('Type') != 'tmpfs' or m.get('Destination') != '/tmp' for m in actual.get('Mounts', []))):
            raise DockerUnavailable('Unexpected sandbox access')
        return config_hash(self.runtime)

    def cleanup(self, job_id):
        if self.deadlines is None or self.deadlines.stop_job(job_id) is not True:
            raise DockerUnavailable('Independent deadline cleanup is not confirmed')
        filters = urlencode({'filters': json.dumps({'label': ['agentx.job=' + job_id]})})
        containers = self.api.request('GET', '/containers/json?all=1&' + filters)
        for item in containers:
            cid = item['Id']
            if (not re.fullmatch('[a-f0-9]{64}', cid)
                    or item.get('Labels', {}).get('agentx.job') != job_id):
                raise DockerUnavailable('Invalid container identity')
            # Force removal terminates all processes. Confirm absence via a second list.
            self.api.request('DELETE', '/containers/' + cid + '?force=1&v=1')
        return self.api.request('GET', '/containers/json?all=1&' + filters) == []

    def case(self, call, index, job_id, register, cancelled, control, job_deadline, authorize, output_budget, approved_digest):
        if self.deadlines is None:
            raise DockerUnavailable('Independent native deadline authority is required before container creation')
        r = self.runtime
        started = time.monotonic()
        deadline = min(started + r.case_timeout_seconds, job_deadline)
        stdout, stderr = bytearray(), bytearray()
        timed_out = overflow = False
        cid = None; attached = None; lease = None; cutoff = None
        state = {}; checked = '0' * 64; cleaned = False
        try:
            with control:
                if cancelled.is_set() or time.monotonic() >= deadline: raise DockerUnavailable('Job stopped')
                authorize()
                spec = container_spec(r)
                spec['Labels'] = {'agentx.job': job_id}
                cid = self.api.request('POST', '/containers/create?' + urlencode({'name': f'agentx-{job_id}-{index}'}), spec)['Id']
                if not re.fullmatch('[a-f0-9]{64}', cid): raise DockerUnavailable('Invalid container identity')
                register(cid)
                checked = self.inspect_config(cid, job_id)
                attached = self.api.attach(cid)
                if cancelled.is_set() or time.monotonic() >= deadline: raise DockerUnavailable('Job stopped')
                authorize()
                # No direct supervisor-issued Docker START. The separate authority
                # must acknowledge a durable native cutoff before starting this CID.
                target = LeaseTarget(job_id=job_id, plan_digest=approved_digest,
                    container_id=cid, runtime_identity=r.identity, config_sha256=checked, case_index=index)
                cutoff = checked_receipt(self.deadlines.arm(target), target, 'armed')
                lease = cutoff['lease_id']
                checked_receipt(self.deadlines.start(lease), target, 'running', started=True,
                                lease_id=lease, attempt_id=cutoff['attempt_id'])
            # Expected answers and tests are never sent to candidate code.
            case = call.cases[index]
            attached.sendall(canonical({'source': call.source, 'function': call.function,
                                       'args': case.args, 'kwargs': case.kwargs}) + b'\n')
            buffer = bytearray()
            while True:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    timed_out = True; break
                authorize()
                try: part = attached.recv(8192)
                except socket.timeout: continue
                if not part: break
                buffer.extend(part)
                while len(buffer) >= 8:
                    stream, size = buffer[0], struct.unpack('>I', buffer[4:8])[0]
                    if stream not in {1, 2} or buffer[1:4] != b'\x00\x00\x00':
                        raise DockerUnavailable('Invalid output framing')
                    if size + len(stdout) + len(stderr) > output_budget:
                        overflow = True; break
                    if len(buffer) < size + 8: break
                    (stdout if stream == 1 else stderr).extend(buffer[8:size+8])
                    del buffer[:size+8]
                if overflow: break
            if not timed_out and not overflow:
                if buffer: raise DockerUnavailable('Truncated output framing')
                state = self.api.request('GET', '/containers/' + cid + '/json')['State']
                if state.get('Running'): raise DockerUnavailable('Output closed while container is still running')
        finally:
            if attached: attached.close()
            # Native cutoff is released only AFTER exact-CID termination/absence.
            # A dead supervisor cannot cancel it; there is no local timer fallback.
            if lease: cutoff = checked_receipt(self.deadlines.finish(lease), target, 'terminated', started=True,
                                              lease_id=lease, attempt_id=cutoff['attempt_id'])
            cleaned = self.cleanup(job_id)
            if not cleaned: raise DockerUnavailable('Container cleanup uncertain')
        duration = int((time.monotonic() - started) * 1000)
        return CaseEvidence(container_id=cid, index=index, exit_code=state.get('ExitCode'),
            stdout=stdout.decode('utf-8', errors='replace'), stderr=stderr.decode('utf-8', errors='replace'),
            timed_out=timed_out, oom=bool(state.get('OOMKilled')), overflow=overflow,
            duration_ms=min(duration, 60000), inspected_config_sha256=checked, stopped=cleaned, removed=cleaned,
            deadline_receipt=cutoff)


class Supervisor:
    """Independent lifecycle owner with durable no-replay ledger and quarantine.

    Construct only in the separately provisioned service. No backend startup imports
    or launches this service. Its runtime is operator-pinned, never model-selected.
    """
    def __init__(self, path, runtime, docker, authorization):
        self.path, self.runtime, self.docker = path, runtime, docker
        self.authorization = authorization
        self.control = threading.Lock()
        self.cancelled = {}
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, digest TEXT NOT NULL, call TEXT NOT NULL, state TEXT NOT NULL, containers TEXT NOT NULL, receipt TEXT)')
        self.recover()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def recover(self):
        with self.db() as db:
            jobs = db.execute("SELECT id FROM jobs WHERE state IN ('running','uncertain')").fetchall()
        for row in jobs:
            # No source submission, creation or start is permitted during recovery.
            self.stop(row['id'])

    def stop(self, job_id):
        if not re.fullmatch('[a-f0-9]{32}', job_id): raise ValueError('Invalid job')
        with self.control:
            event = self.cancelled.get(job_id)
            if event: event.set()
            with self.db() as db:
                row = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
                if not row:
                    # Submission may have been lost before the supervisor ledger
                    # commit. Reconcile by label, never by replaying that request.
                    db.execute("INSERT INTO jobs VALUES (?,?,'{}','uncertain','[]',NULL)", (job_id, '0'*64))
                else:
                    db.execute("UPDATE jobs SET state='uncertain' WHERE id=?", (job_id,))
            try: cleaned = self.docker.cleanup(job_id)
            except Exception: cleaned = False
            with self.db() as db:
                db.execute('UPDATE jobs SET state=? WHERE id=?', ('stopped' if cleaned else 'uncertain', job_id))
            return cleaned

    def receipt(self, job_id):
        with self.db() as db:
            row = db.execute('SELECT receipt FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or not row['receipt']: raise ValueError('No complete receipt')
        return json.loads(row['receipt'])

    def run(self, job_id, digest, call):
        if not re.fullmatch('[a-f0-9]{32}', job_id) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('Invalid job identity')
        call = PythonCall.model_validate(call.model_dump())
        if call.runtime != self.runtime: raise ValueError('Runtime mismatch')
        self.authorization(job_id, digest, call)
        self.docker.preflight()
        cancelled = threading.Event()
        with self.control:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','uncertain')").fetchone():
                    raise ValueError('Sandbox busy or quarantined')
                db.execute("INSERT INTO jobs VALUES (?,?,?,'running','[]',NULL)", (job_id, digest, call.model_dump_json()))
            self.cancelled[job_id] = cancelled
        started_at = now(); cases = []; error = None; cleaned = False
        deadline = time.monotonic() + call.runtime.job_timeout_seconds
        def register(cid):
            with self.db() as db:
                row = db.execute('SELECT containers FROM jobs WHERE id=?', (job_id,)).fetchone()
                db.execute('UPDATE jobs SET containers=? WHERE id=?', (json.dumps(json.loads(row['containers']) + [cid]), job_id))
        try:
            for index in range(len(call.cases)):
                if cancelled.is_set() or time.monotonic() >= deadline: raise DockerUnavailable('Job deadline/interruption')
                budget = call.runtime.output_bytes - sum(len(c.stdout.encode()) + len(c.stderr.encode()) for c in cases)
                result = self.docker.case(call, index, job_id, register, cancelled, self.control, deadline,
                                          lambda: self.authorization(job_id, digest, call), budget, digest)
                cases.append(result)
                if result.exit_code != 0 or result.timed_out or result.oom or result.overflow: break
            cleaned = self.docker.cleanup(job_id)
            if not cleaned: raise DockerUnavailable('Cleanup not confirmed')
        except BaseException:
            error = 'Sandbox interrupted or uncertain; source is never replayed.'
            try: cleaned = self.docker.cleanup(job_id)
            except Exception: cleaned = False
        receipt = PythonEvidence(job_id=job_id, plan_digest=digest, source_sha256=call.source_sha256,
            tests_sha256=call.tests_sha256, runtime=call.runtime, started_at=started_at, finished_at=now(),
            cases=cases, cleanup_confirmed=cleaned, error=error)
        with self.control:
            if cancelled.is_set():
                receipt.error = 'Sandbox cancelled or ownership lost; result cannot complete.'
            with self.db() as db:
                db.execute('UPDATE jobs SET state=?,receipt=? WHERE id=?',
                           ('finished' if cleaned else 'uncertain', receipt.model_dump_json(), job_id))
            self.cancelled.pop(job_id, None)
        return receipt
