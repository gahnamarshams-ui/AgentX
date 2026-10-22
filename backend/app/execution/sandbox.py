"""Fail-closed client for an independently operated Unix-socket supervisor.

No Docker CLI, shell, image pull, host subprocess or candidate evaluation here.
The supervisor must be provisioned separately; configuration is not proof of isolation.
"""
import http.client
import json
import os
import socket
import stat
from app.execution.python_models import PythonCall, PythonEvidence, canonical, fingerprint
from app.execution.security import ExecutionError, check_secrets

# Deny by default, including network sockets, mounts, ptrace and namespace changes.
# The final image/profile combination requires separately authorized live validation.
SECCOMP = {'defaultAction': 'SCMP_ACT_ERRNO', 'syscalls': [{
    'action': 'SCMP_ACT_ALLOW', 'names': [
        'read', 'write', 'readv', 'writev', 'close', 'close_range', 'fstat', 'newfstatat',
        'stat', 'lstat', 'statx', 'lseek', 'pread64', 'pwrite64', 'openat', 'open',
        'fcntl', 'ioctl', 'getdents64', 'readlink', 'readlinkat', 'access', 'faccessat',
        'faccessat2', 'getcwd', 'chdir', 'mmap', 'mprotect', 'munmap', 'mremap',
        'madvise', 'brk', 'rt_sigaction', 'rt_sigprocmask', 'rt_sigreturn',
        'sigaltstack', 'futex', 'set_tid_address', 'set_robust_list', 'rseq',
        'arch_prctl', 'prlimit64', 'getrlimit', 'getrandom', 'clock_gettime',
        'clock_nanosleep', 'nanosleep', 'gettimeofday', 'getpid', 'getppid',
        'gettid', 'getuid', 'geteuid', 'getgid', 'getegid', 'getgroups', 'uname',
        'sysinfo', 'sched_getaffinity', 'sched_yield', 'poll', 'ppoll', 'select',
        'pselect6', 'dup', 'dup2', 'dup3', 'execve', 'exit', 'exit_group',
    ]}]}
ENV = ['PATH=/usr/local/bin:/usr/bin:/bin', 'LANG=C.UTF-8']


def container_spec(runtime):
    return {
        'Image': runtime.image, 'User': runtime.user,
        'Entrypoint': ['/usr/local/bin/python3'], 'Cmd': ['-I', '-B', '/runner.py'],
        'Env': ENV, 'WorkingDir': '/tmp', 'OpenStdin': True, 'StdinOnce': True,
        'AttachStdin': True, 'AttachStdout': True, 'AttachStderr': True, 'Tty': False,
        'NetworkDisabled': True,
        'Healthcheck': {'Test': ['NONE']},
        'HostConfig': {
            'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
            'CapDrop': ['ALL'], 'CapAdd': [], 'Binds': [], 'Mounts': [],
            'Devices': [], 'DeviceRequests': [], 'VolumesFrom': [],
            'PidMode': '', 'IpcMode': 'none', 'UTSMode': '', 'CgroupnsMode': 'private',
            'SecurityOpt': ['no-new-privileges:true', 'seccomp=' + canonical(SECCOMP).decode()],
            'NanoCpus': runtime.cpus * 1000000000, 'Memory': runtime.memory_bytes,
            'MemorySwap': runtime.memory_swap_bytes, 'PidsLimit': runtime.pids,
            'Tmpfs': {'/tmp': f'rw,noexec,nosuid,nodev,size={runtime.tmpfs_bytes},mode=1777'},
            'Ulimits': [{'Name': 'nofile', 'Soft': 64, 'Hard': 64},
                        {'Name': 'core', 'Soft': 0, 'Hard': 0},
                        {'Name': 'cpu', 'Soft': 5, 'Hard': 5}],
            'RestartPolicy': {'Name': 'no'}, 'LogConfig': {'Type': 'none', 'Config': {}},
            'AutoRemove': False,
        },
    }


def config_hash(runtime):
    return fingerprint(container_spec(runtime))


def configured_runtime(settings):
    """Resolve operator policy only; never probe a runtime during planning."""
    from app.execution.python_models import PythonRuntime
    if not settings.python_execution_enabled:
        raise ExecutionError('python_disabled', 'Python sandbox execution is disabled.', 503)
    if not all([settings.sandbox_runtime_identity, settings.sandbox_runner_image,
                settings.sandbox_bootstrap_sha256, settings.sandbox_seccomp_sha256,
                settings.sandbox_supervisor_socket, settings.sandbox_deadline_socket]):
        raise ExecutionError('sandbox_untrusted', 'Pinned sandbox configuration is required.', 503)
    return PythonRuntime(policy='agentx-python-v1', identity=settings.sandbox_runtime_identity,
        image=settings.sandbox_runner_image, bootstrap_sha256=settings.sandbox_bootstrap_sha256,
        seccomp_sha256=settings.sandbox_seccomp_sha256, network='none', host_mounts=False,
        inherit_environment=False, user='65532:65532', read_only=True, cpus=1,
        memory_bytes=268435456, memory_swap_bytes=268435456, pids=16,
        tmpfs_bytes=16777216, output_bytes=65536, case_timeout_seconds=5, job_timeout_seconds=30)


def assess_python(settings, call):
    call = PythonCall.model_validate(call.model_dump())
    if not settings.python_execution_enabled:
        raise ExecutionError('python_disabled', 'Python sandbox execution is disabled.', 503)
    expected = (settings.sandbox_runtime_identity, settings.sandbox_runner_image,
                settings.sandbox_bootstrap_sha256, settings.sandbox_seccomp_sha256)
    actual = (call.runtime.identity, call.runtime.image,
              call.runtime.bootstrap_sha256, call.runtime.seccomp_sha256)
    if (not all(expected) or expected != actual or settings.sandbox_supervisor_socket is None
            or settings.sandbox_deadline_socket is None
            or call.runtime.seccomp_sha256 != fingerprint(SECCOMP)):
        raise ExecutionError('sandbox_untrusted', 'A pinned, trusted sandbox configuration is required.', 503)
    check_secrets(settings, call.model_dump_json())


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            raise


class SupervisorClient:
    def __init__(self, settings): self.settings = settings

    def request(self, method, path, body=None):
        location = self.settings.sandbox_supervisor_socket
        if location is None:
            raise ExecutionError('sandbox_unavailable', 'Sandbox supervisor is not configured.', 503)
        conn = None
        try:
            info = location.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Untrusted supervisor socket')
            conn = UnixHTTP(location, 35)
            conn.request(method, path, body=canonical(body) if body is not None else None,
                         headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            data = response.read(1500001)
            if response.status != 200 or len(data) > 1500000:
                raise ValueError('Supervisor refused request')
            return json.loads(data)
        except (OSError, ValueError, http.client.HTTPException):
            raise ExecutionError('sandbox_unavailable', 'Sandbox unavailable or uncertain; no automatic replay is permitted.', 503) from None
        finally:
            if conn: conn.close()

    def preflight(self, runtime):
        info = self.request('GET', '/health')
        if info != {'runtime': runtime.model_dump(), 'ready': True}:
            raise ExecutionError('sandbox_untrusted', 'Sandbox runtime identity or isolation configuration does not match.', 503)

    def run(self, job_id, digest, call):
        assess_python(self.settings, call)
        self.preflight(call.runtime)
        return PythonEvidence.model_validate(self.request('POST', '/jobs', {
            'job_id': job_id, 'plan_digest': digest, 'call': call.model_dump()}))

    def receipt(self, job_id):
        return PythonEvidence.model_validate(self.request('GET', '/jobs/' + job_id))

    def deadline_receipts(self, evidence):
        from app.execution.deadlines import DeadlineClient
        client = DeadlineClient(self.settings.sandbox_deadline_socket)
        if any(not case.deadline_receipt for case in evidence.cases):
            raise ExecutionError('deadline_evidence_missing', 'Native cutoff receipts are required.', 502)
        return [client.receipt(case.deadline_receipt['lease_id']) for case in evidence.cases]

    def stop(self, job_id):
        # Cancellation/reconciliation only: never starts a job or repeats source.
        return self.request('POST', '/stop/' + job_id) == {'cleanup_confirmed': True}
