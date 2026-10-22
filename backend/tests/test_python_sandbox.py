"""Mock-only Python sandbox contract tests. Candidate strings are NEVER evaluated.

No Docker/VM, host process, live workspace tool, or external network is invoked.
All approvals below concern in-process TestClient fixtures and fake runtime jobs.
"""
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest
pytest.importorskip('fcntl', reason='POSIX supervisor contracts require fcntl; NOT TESTED on Windows')
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.database import connection
from app.execution.agents import PolicySafety, WorkspaceExecutor, OpenAIPlanner, PythonProposal
from app.execution.docker_supervisor import DockerAPI, DockerRuntime, DockerUnavailable, Supervisor
from app.execution.models import ExecutionPlan
from app.execution.python_models import PythonCall, PythonEvidence, CaseEvidence, canonical, fingerprint, verify_python
from app.execution.python_workflow import recover_python_jobs, mark_stale_python_jobs
from app.execution.sandbox import SupervisorClient, SECCOMP, ENV, configured_runtime, container_spec, config_hash
from app.execution.store import digest, recover
from app.execution.supervisor_service import approved_job
from app.execution.workspace import Workspace
from app.main import create_app

SOURCE = 'def add(a, b):\n    return a + b\n'  # Opaque bytes, not executable test code.


@pytest.fixture(autouse=True)
def forbid_live_operations(monkeypatch):
    original_request = SupervisorClient.request
    def forbidden(*args, **kwargs): pytest.fail('Live runtime/workspace tools forbidden in this suite')
    for cls, method in [(DockerAPI, 'request'), (DockerAPI, 'attach'),
                        (SupervisorClient, 'request'), (Workspace, 'read'), (Workspace, 'create'),
                        (WorkspaceExecutor, 'execute'), (WorkspaceExecutor, 'execute_step')]:
        monkeypatch.setattr(cls, method, forbidden)
    yield original_request


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / 'fixture-workspace'
    root.mkdir()  # Explicit fixture provisioning, no application workspace tools.
    return Settings(_env_file=None, database_path=tmp_path/'backend.sqlite3', workspace_path=root,
        execution_enabled=True, python_execution_enabled=True, openai_api_key='test-placeholder-only',
        sandbox_supervisor_socket=tmp_path/'not-started.sock', sandbox_runtime_identity='mock-dedicated-macos-vm',
        sandbox_deadline_socket=tmp_path/'not-started-native.sock',
        sandbox_runner_image='sha256:'+'a'*64, sandbox_bootstrap_sha256='b'*64,
        sandbox_seccomp_sha256=fingerprint(SECCOMP))


def plan_for(settings):
    cases = [dict(args=[2, 3], kwargs={}, expected=5), dict(args=[-1, 1], kwargs={}, expected=0)]
    call = dict(name='run_python_function', source=SOURCE, function='add', cases=cases,
        runtime=configured_runtime(settings).model_dump(), source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        tests_sha256=fingerprint(cases))
    return ExecutionPlan.model_validate(dict(goal='Check addition', assumptions=[], constraints=['Sandbox only'],
        clarification_needed=False, clarification_questions=[], workflow='python', steps=[
            dict(number=1, action='Test add', purpose='Check approved examples', expected_output='Actual test evidence',
                 dependencies=[], tool=call)]))


def evidence(call, job_id, saved_digest):
    return PythonEvidence(job_id=job_id, plan_digest=saved_digest, source_sha256=call.source_sha256,
        tests_sha256=call.tests_sha256, runtime=call.runtime, started_at='2026-01-01T00:00:00+00:00',
        finished_at='2026-01-01T00:00:01+00:00', cleanup_confirmed=True, error=None, cases=[
            CaseEvidence(container_id=str(i+1)*64, index=i, exit_code=0,
                stdout=json.dumps({'value': case.expected}), stderr='', timed_out=False,
                oom=False, overflow=False, duration_ms=10, inspected_config_sha256=config_hash(call.runtime),
                stopped=True, removed=True, deadline_receipt={
                    'job_id':job_id, 'plan_digest':saved_digest, 'container_id':str(i+1)*64,
                    'runtime_identity':call.runtime.identity, 'config_sha256':config_hash(call.runtime),
                    'case_index':i, 'authority_id':call.runtime.identity+':deadlines-v1',
                    'lease_id':str(i+1)*32, 'boot_id':'fake-native-boot', 'armed_at':float(i),
                    'expires_at':float(i+5), 'job_expires_at':30.0,
                    'state':'terminated','termination_confirmed':True, 'start_confirmed':True,
                    'receipt_version':2, 'attempt_id':str(i+3)*32, 'registered_at':float(i),
                    'deadline_met':True, 'timing':{
                        'job_id':job_id, 'plan_digest':saved_digest, 'container_id':str(i+1)*64,
                        'runtime_identity':call.runtime.identity, 'config_sha256':config_hash(call.runtime),
                        'case_index':i, 'lease_id':str(i+1)*32, 'attempt_id':str(i+3)*32,
                        'boot_id':'fake-native-boot','source':'native-runtime-observer',
                        'cutoff_requested_at':None,'workload_stopped_at':float(i)+0.01,
                        'termination_confirmed_at':float(i)+0.02,'reason':'completed',
                    },
                }) for i, case in enumerate(call.cases)])


class FakeSandbox:
    def __init__(self):
        self.calls = []; self.receipts = {}; self.stops = []
        self.preflight = MagicMock(); self.change = None; self.change_trusted = None
        self.clean = True; self.pending = None

    def run(self, job, saved_digest, call):
        self.calls.append(job)
        if self.pending: self.pending()
        receipt = evidence(call, job, saved_digest)
        if self.change: self.change(receipt)
        self.receipts[job] = receipt.model_copy(deep=True)
        if self.change_trusted: self.change_trusted(self.receipts[job])
        return receipt

    def receipt(self, job): return self.receipts[job].model_copy(deep=True)
    def deadline_receipts(self, receipt):
        return [copy.deepcopy(case.deadline_receipt) for case in receipt.cases]
    def stop(self, job): self.stops.append(job); return self.clean


@pytest.fixture
def setup(settings):
    sandbox = FakeSandbox()
    with TestClient(create_app(settings), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        o = client.app.state.orchestrator
        o.sandbox = sandbox
        o.planner = SimpleNamespace(plan_python=AsyncMock(return_value=plan_for(settings)))
        yield settings, client, o, sandbox


def prepare(client):
    url = '/api/tasks/' + client.post('/api/tasks', json={'description': 'Mock sandbox test only'}).json()['id']
    result = client.post(url+'/orchestrate?workflow=python')
    assert result.status_code == 200, result.text
    return url, result.json()


def approve(client, url, run):
    return client.post(url+'/approval', json={'plan_digest': run['plan_digest'], 'decision': 'approve'})


def test_disabled_defaults_and_no_supervisor_calls(tmp_path):
    s = Settings(_env_file=None, database_path=tmp_path/'db.sqlite3', workspace_path=tmp_path/'absent')
    assert s.execution_enabled is False and s.python_execution_enabled is False
    with TestClient(create_app(s), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as c:
        task = c.post('/api/tasks', json={'description': 'Never execute'}).json()['id']
        assert c.post('/api/tasks/'+task+'/orchestrate?workflow=python').status_code == 503
    assert not s.workspace_path.exists()


def test_preparation_missing_rejected_mismatched_approval_zero_runtime_calls(setup):
    s, c, o, sandbox = setup
    before = list(s.workspace_path.iterdir())
    url, run = prepare(c)
    sandbox.preflight.assert_not_called(); assert sandbox.calls == []
    for suffix in ['/execution', '/activity', '/history']:
        assert c.get(url+suffix).status_code == 200
    assert c.post(url+'/approval', json={'decision':'approve'}).status_code == 422
    assert approve(c, url, {'plan_digest':'0'*64}).status_code == 409
    assert c.post(url+'/approval', json={'plan_digest':run['plan_digest'], 'decision':'reject'}).json()['phase'] == 'cancelled'
    assert approve(c, url, run).status_code == 409
    assert sandbox.calls == [] and sandbox.stops == []
    sandbox.preflight.assert_not_called()
    assert list(s.workspace_path.iterdir()) == before


@pytest.mark.parametrize('field', ['source', 'tests', 'image', 'identity', 'bootstrap', 'seccomp'])
def test_plan_digest_tampering_never_calls_runtime(setup, field):
    s, c, o, sandbox = setup
    url, run = prepare(c)
    changed = plan_for(s).model_dump()
    call = changed['steps'][0]['tool']
    if field == 'source':
        call['source'] += '# changed\n'; call['source_sha256'] = hashlib.sha256(call['source'].encode()).hexdigest()
    elif field == 'tests':
        call['cases'][0]['expected'] = 6; call['tests_sha256'] = fingerprint(call['cases'])
    else:
        key = {'image':'image', 'identity':'identity', 'bootstrap':'bootstrap_sha256','seccomp':'seccomp_sha256'}[field]
        call['runtime'][key] = ('sha256:' if field == 'image' else '') + 'c'*64
    changed = ExecutionPlan.model_validate(changed)
    assert digest(changed, o.workspace.identity) != run['plan_digest']
    with connection(s.database_path) as db:
        db.execute('UPDATE execution_runs SET plan_json=? WHERE task_id=?', (changed.model_dump_json(), run['task_id']))
        db.execute('UPDATE execution_steps SET tool_json=? WHERE task_id=?', (changed.steps[0].tool.model_dump_json(), run['task_id']))
    assert approve(c, url, run).status_code == 409
    sandbox.preflight.assert_not_called(); assert sandbox.calls == []


@pytest.mark.parametrize('key,value', [('network','bridge'), ('host_mounts',True), ('inherit_environment',True),
    ('user','0:0'), ('read_only',False), ('cpus',2), ('memory_bytes',536870912),
    ('memory_swap_bytes',536870912), ('pids',100), ('tmpfs_bytes',33554432),
    ('output_bytes',131072), ('case_timeout_seconds',60), ('job_timeout_seconds',300)])
def test_permissions_and_resource_limit_overrides_are_rejected(settings, key, value):
    data = plan_for(settings).model_dump()
    data['steps'][0]['tool']['runtime'][key] = value
    with pytest.raises(ValidationError): ExecutionPlan.model_validate(data)


def test_hashes_and_immutable_image_required(settings):
    data = plan_for(settings).model_dump()
    data['steps'][0]['tool']['source'] += '# unapproved'
    with pytest.raises(ValidationError): ExecutionPlan.model_validate(data)
    with pytest.raises(ValidationError): Settings(_env_file=None, sandbox_runner_image='python:latest')


def test_python_policy_is_separate_and_operator_pinned(settings):
    plan = plan_for(settings)
    assert PolicySafety(settings).assess(plan)
    settings.python_execution_enabled = False
    with pytest.raises(Exception, match='disabled'): PolicySafety(settings).assess(plan)
    settings.python_execution_enabled = True
    settings.sandbox_runner_image = 'sha256:'+'d'*64
    with pytest.raises(Exception, match='pinned'): PolicySafety(settings).assess(plan)


def test_mock_success_persists_exact_five_roles_and_independent_receipt(setup):
    s, c, o, sandbox = setup
    url, run = prepare(c)
    result = approve(c, url, run)
    assert result.status_code == 200, result.text
    result = result.json()
    assert result['phase'] == 'completed'
    assert [a['role'] for a in result['agents']] == ['Orchestrator','Planner','Safety','Executor','Verifier']
    assert all(a['status']=='succeeded' for a in result['agents'])
    assert result['steps'][0]['attempts'] == 1 and result['steps'][0]['result']['kind'] == 'python'
    assert result['agents'][-1]['sandbox_evidence'][0]['source_sha256'] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert 'approved_json_cases_match' in result['agents'][-1]['checks']
    assert len(sandbox.calls) == 1
    assert c.get(url+'/execution').json() == result
    assert c.get(url+'/activity').json() == result
    messages = ' '.join(e['message'] for e in c.get(url+'/history').json()['events'])
    assert all('['+role+']' in messages for role in ['Orchestrator','Planner','Safety','Executor','Verifier'])
    assert approve(c, url, run).status_code == 409
    assert len(sandbox.calls) == 1
    with connection(s.database_path) as db:
        assert db.execute('SELECT state FROM python_jobs').fetchone()[0] == 'completed'


@pytest.mark.parametrize('failure', ['timeout','oom','overflow','nonzero','cleanup','fabrication','printed_pass','wrong_answer','config'])
def test_simulated_failures_and_fabricated_evidence_cannot_complete(setup, failure):
    s, c, o, sandbox = setup
    def change(e):
        case = e.cases[0]
        if failure == 'timeout': case.timed_out = True
        elif failure == 'oom': case.oom = True
        elif failure == 'overflow': case.overflow = True
        elif failure == 'nonzero': case.exit_code = 1
        elif failure == 'cleanup': e.cleanup_confirmed = False
        elif failure == 'printed_pass': case.stdout = '{"passed":true}'
        elif failure == 'wrong_answer': case.stdout = '{"value":999}'
        elif failure == 'config': case.inspected_config_sha256 = '0'*64
    sandbox.change = change
    if failure == 'fabrication': sandbox.change_trusted = lambda e: setattr(e.cases[0], 'stdout', '{"value":999}')
    if failure == 'cleanup': sandbox.clean = False
    url, run = prepare(c)
    assert approve(c, url, run).status_code == 502
    assert c.get(url).json()['status'] == 'failed'
    assert c.get(url).json()['final_result'] is None
    assert len(sandbox.calls) == 1 and len(sandbox.stops) == 1
    with connection(s.database_path) as db:
        state = db.execute('SELECT state,evidence FROM python_jobs').fetchone()
        assert state['state'] == ('uncertain' if failure == 'cleanup' else 'failed')
        assert state['evidence'] is not None
    if failure == 'cleanup':
        assert c.delete(url).status_code == 409
        url2, run2 = prepare(c)
        assert approve(c, url2, run2).status_code == 502
        assert len(sandbox.calls) == 1


def test_runtime_unavailable_fails_before_source_submission(setup):
    s, c, o, sandbox = setup
    sandbox.preflight.side_effect = RuntimeError('Unavailable fake runtime')
    url, run = prepare(c)
    assert approve(c, url, run).status_code == 502
    assert sandbox.calls == [] and sandbox.stops == []
    assert c.get(url).json()['status'] == 'failed'


def test_db_lock_released_and_duplicate_approval_cannot_start_twice(setup):
    s, c, o, sandbox = setup
    entered, release = threading.Event(), threading.Event()
    def pending():
        entered.set(); assert release.wait(5)
    sandbox.pending = pending
    url, run = prepare(c)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(approve, c, url, run)
        assert entered.wait(5)
        try:
            # A write transaction succeeds while the fake sandbox is pending.
            with connection(s.database_path) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE tasks SET title='Still responsive' WHERE id=?", (run['task_id'],))
            assert approve(c, url, run).status_code == 409
            assert c.get(url+'/activity').status_code == 200
        finally: release.set()
        assert first.result().json()['phase'] == 'completed'
    assert len(sandbox.calls) == 1


def test_lost_ownership_stops_job_and_never_saves_success(setup):
    s, c, o, sandbox = setup
    url, run = prepare(c)
    def steal():
        with connection(s.database_path) as db:
            db.execute("UPDATE execution_runs SET owner='different-owner' WHERE task_id=?", (run['task_id'],))
    sandbox.pending = steal
    assert approve(c, url, run).status_code == 502
    assert c.get(url).json()['final_result'] is None
    assert len(sandbox.stops) == 1 and len(sandbox.calls) == 1
    mark_stale_python_jobs(s.database_path)
    recover_python_jobs(s, sandbox)
    assert len(sandbox.calls) == 1


def test_interruption_recovery_stops_only_and_quarantines_failed_cleanup(setup):
    s, c, o, sandbox = setup
    url, run = prepare(c)
    # Simulate durable submission intent from a crashed backend, without running source.
    from app.execution.models import ApprovalRequest
    owner = o.approve(run['task_id'], ApprovalRequest(plan_digest=run['plan_digest'], decision='approve'))
    job = uuid4().hex
    with connection(s.database_path) as db:
        db.execute("INSERT INTO python_jobs VALUES (?,?,?,'submitted',NULL,?,?)", (run['task_id'], job, owner, 'x', 'x'))
        db.execute("UPDATE execution_runs SET expires_at='2000-01-01' WHERE task_id=?", (run['task_id'],))
    recover(s.database_path)
    sandbox.clean = False
    recover_python_jobs(s, sandbox)
    with connection(s.database_path) as db:
        assert db.execute('SELECT state FROM python_jobs').fetchone()[0] == 'uncertain'
    assert sandbox.calls == [] and sandbox.stops == [job]
    sandbox.clean = True
    recover_python_jobs(s, sandbox)
    with connection(s.database_path) as db:
        assert db.execute('SELECT state FROM python_jobs').fetchone()[0] == 'failed'
    assert sandbox.calls == []


def test_legacy_digest_serialization_unchanged(settings):
    for workflow in [None, 'workspace']:
        data = plan_for(settings).model_dump()
        data['workflow'] = workflow
        data['steps'][0]['tool'] = dict(name='read_text', path='data.json', content=None, required_keys=[])
        plan = ExecutionPlan.model_validate(data)
        legacy = plan.model_dump(); legacy.pop('workflow')
        old = hashlib.sha256(json.dumps({'plan':legacy, 'workspace':'existing-root'}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert digest(plan, 'existing-root') == old


def test_docker_spec_has_no_mounts_or_environment_inheritance(settings):
    runtime = configured_runtime(settings)
    spec = container_spec(runtime); host = spec['HostConfig']
    assert spec['Env'] == ENV and spec['User'] == '65532:65532'
    assert host['Binds'] == host['Mounts'] == host['Devices'] == []
    assert host['NetworkMode'] == 'none' and spec['NetworkDisabled']
    assert host['ReadonlyRootfs'] and not host['Privileged']
    assert host['Memory'] == host['MemorySwap'] == 268435456
    assert host['PidsLimit'] == 16 and host['NanoCpus'] == 1000000000
    assert SECCOMP['defaultAction'] == 'SCMP_ACT_ERRNO'
    allowed = SECCOMP['syscalls'][0]['names']
    assert not set(['socket','connect','ptrace','mount','unshare','clone','clone3']) & set(allowed)


def test_no_socket_fails_closed_without_creating_it(settings, monkeypatch, forbid_live_operations):
    client = SupervisorClient(settings)
    monkeypatch.setattr(client, 'request', forbid_live_operations.__get__(client))
    with pytest.raises(Exception, match='unavailable'): client.preflight(configured_runtime(settings))
    assert not settings.sandbox_supervisor_socket.exists()


def test_supervisor_preflight_identity_and_configuration_must_match(settings, monkeypatch):
    client = SupervisorClient(settings)
    monkeypatch.setattr(client, 'request', lambda *a: {'runtime': {}, 'ready': True})
    with pytest.raises(Exception, match='identity'): client.preflight(configured_runtime(settings))


def test_planner_hashes_source_without_evaluating_or_probing_runtime(settings, monkeypatch):
    import asyncio
    from app import ai
    proposal = PythonProposal(goal='Addition', assumptions=[], constraints=[], clarification_needed=False,
        clarification_questions=[], steps=[dict(number=1, action='Check', purpose='Explain', expected_output='Result', dependencies=[])],
        source=SOURCE, function='add', cases=[dict(args=[1,2], kwargs={}, expected=3)])
    generate = AsyncMock(return_value=proposal)
    monkeypatch.setattr(ai, 'generate_structured', generate)
    plan = asyncio.run(OpenAIPlanner(settings).plan_python('Prepare addition cases'))
    assert plan.steps[0].tool.source_sha256 == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert PolicySafety(settings).assess(plan)
    assert generate.await_count == 1


def test_standalone_supervisor_recovery_never_submits_source(tmp_path, settings):
    runtime = configured_runtime(settings)
    docker = MagicMock(); docker.cleanup.return_value = False
    authorize = MagicMock()
    supervisor = Supervisor(tmp_path/'supervisor.sqlite3', runtime, docker, authorize)
    job = uuid4().hex
    with supervisor.db() as db:
        db.execute("INSERT INTO jobs VALUES (?,?,'{}','running','[]',NULL)", (job, '0'*64))
    supervisor.recover()
    docker.cleanup.assert_called_with(job)
    docker.case.assert_not_called(); docker.preflight.assert_not_called(); authorize.assert_not_called()
    with supervisor.db() as db: assert db.execute('SELECT state FROM jobs').fetchone()[0] == 'uncertain'


def test_supervisor_independent_authorization_and_durable_duplicate_prevention(tmp_path, settings):
    runtime = configured_runtime(settings); call = plan_for(settings).steps[0].tool
    docker = MagicMock(); docker.cleanup.return_value = True
    def case(call, index, job, *args): return evidence(call, job, '0'*64).cases[index]
    docker.case.side_effect = case
    supervisor = Supervisor(tmp_path/'supervisor.sqlite3', runtime, docker, MagicMock())
    job = uuid4().hex
    result = supervisor.run(job, '0'*64, call)
    assert result.cleanup_confirmed and len(result.cases) == 2
    assert supervisor.receipt(job)['source_sha256'] == call.source_sha256
    with pytest.raises(Exception): supervisor.run(job, '0'*64, call)
    assert docker.case.call_count == 2


def test_supervisor_refuses_source_without_backend_approval(tmp_path, settings):
    docker = MagicMock()
    denied = MagicMock(side_effect=ValueError('No approval'))
    supervisor = Supervisor(tmp_path/'supervisor.sqlite3', configured_runtime(settings), docker, denied)
    with pytest.raises(ValueError): supervisor.run(uuid4().hex, '0'*64, plan_for(settings).steps[0].tool)
    docker.preflight.assert_not_called(); docker.case.assert_not_called()


def test_live_activation_is_blocked_even_with_a_configured_runtime(settings):
    api = MagicMock()
    runtime = DockerRuntime(configured_runtime(settings), api)
    with pytest.raises(DockerUnavailable, match='crash-independent deadline'):
        runtime.preflight()
    api.request.assert_not_called(); api.attach.assert_not_called()


def metadata_api(runtime):
    info = dict(ID='daemon', OSType='linux', Architecture='arm64', OperatingSystem='Docker Desktop',
        MemoryLimit=True, SwapLimit=True, PidsLimit=True, CpuCfsQuota=True, SecurityOptions=['name=seccomp,profile=builtin'])
    version = dict(Version='test-version', KernelVersion='test-kernel')
    image = dict(Id=runtime.image, Architecture='arm64', Os='linux', Config={
        'Env': ENV, 'Volumes': None, 'Labels': {'agentx.bootstrap.sha256':runtime.bootstrap_sha256}})
    values = {'/info':info, '/version':version, '/images/'+runtime.image+'/json':image}
    api = MagicMock(); api.request.side_effect = lambda method, path, *a: values[path]
    return api, values


@pytest.mark.parametrize('change', ['none','identity','memory','swap','processes','cpu','image','env','volume','bootstrap'])
def test_mock_metadata_checks_fail_closed_on_missing_isolation(settings, change):
    runtime = configured_runtime(settings).model_copy(update={'identity':'docker:daemon:test-version:test-kernel:linux/arm64'})
    api, values = metadata_api(runtime)
    image = values['/images/'+runtime.image+'/json']
    if change == 'identity': values['/info']['ID'] = 'another-daemon'
    elif change in {'memory','swap','processes','cpu'}:
        key = {'memory':'MemoryLimit','swap':'SwapLimit','processes':'PidsLimit','cpu':'CpuCfsQuota'}[change]
        values['/info'][key] = False
    elif change == 'image': image['Id'] = 'sha256:'+'f'*64
    elif change == 'env': image['Config']['Env'] = ENV + ['UNEXPECTED=not-allowed']
    elif change == 'volume': image['Config']['Volumes'] = {'/host':{}}
    elif change == 'bootstrap': image['Config']['Labels'] = {}
    adapter = DockerRuntime(runtime, api)
    if change == 'none': adapter.inspect_environment()
    else:
        with pytest.raises(DockerUnavailable): adapter.inspect_environment()
    assert all(args[0] == 'GET' for args, kwargs in api.request.call_args_list)


@pytest.mark.parametrize('change', ['approval','ownership','source','step','none'])
def test_supervisor_reads_exact_backend_approval_independently(setup, change):
    s, c, o, sandbox = setup
    url, run = prepare(c)
    from app.execution.models import ApprovalRequest
    owner = o.approve(run['task_id'], ApprovalRequest(plan_digest=run['plan_digest'], decision='approve'))
    job = uuid4().hex
    with connection(s.database_path) as db:
        db.execute("INSERT INTO python_jobs VALUES (?,?,?,'submitted',NULL,'x','x')", (run['task_id'], job, owner))
        db.execute("UPDATE execution_steps SET status='executing',attempts=1 WHERE task_id=?", (run['task_id'],))
        if change == 'approval': db.execute('DELETE FROM execution_approvals WHERE task_id=?', (run['task_id'],))
        elif change == 'ownership': db.execute("UPDATE execution_runs SET owner='stolen' WHERE task_id=?", (run['task_id'],))
        elif change == 'source': db.execute("UPDATE execution_runs SET plan_digest=? WHERE task_id=?", ('0'*64, run['task_id']))
        elif change == 'step': db.execute("UPDATE execution_steps SET attempts=2 WHERE task_id=?", (run['task_id'],))
    if change == 'none': approved_job(s.database_path, job, run['plan_digest'], plan_for(s).steps[0].tool)
    else:
        with pytest.raises(ValueError): approved_job(s.database_path, job, run['plan_digest'], plan_for(s).steps[0].tool)
    assert sandbox.calls == []


def test_existing_file_flow_with_fake_tools_only(setup):
    """Exercise the legacy orchestration contract, not actual workspace I/O."""
    from app.execution.models import ToolResult, Verification
    s, c, o, sandbox = setup
    data = plan_for(s).model_dump(); data['workflow'] = 'workspace'
    data['steps'][0]['tool'] = dict(name='write_text',path='output.json',content='{"ok":true}',required_keys=[])
    legacy = ExecutionPlan.model_validate(data)
    o.planner = SimpleNamespace(plan=AsyncMock(return_value=legacy))
    o.executor = SimpleNamespace(execute=MagicMock(return_value=ToolResult(path='output.json',
        sha256=hashlib.sha256(b'{"ok":true}').hexdigest(),byte_count=11)))
    o.verifier = SimpleNamespace(verify=MagicMock(return_value=Verification(passed=True,checks=['mock_only'])))
    task = c.post('/api/tasks',json={'description':'Legacy mock only'}).json()['id']
    url = '/api/tasks/'+task
    run = c.post(url+'/orchestrate').json()
    assert approve(c,url,run).json()['phase'] == 'completed'
    assert not (s.workspace_path/'output.json').exists()
    assert sandbox.calls == []


@pytest.mark.parametrize('change', ['duplicate_json','boolean_number','elapsed','oversize','duplicate_container'])
def test_untrusted_output_cannot_fabricate_passing_test_evidence(settings, change):
    call = plan_for(settings).steps[0].tool
    receipt = evidence(call, uuid4().hex, '0'*64)
    if change == 'duplicate_json': receipt.cases[0].stdout = '{"value":1,"value":5}'
    elif change == 'boolean_number': receipt.cases[1].stdout = '{"value":false}'
    elif change == 'elapsed': receipt.finished_at = '2026-01-01T00:01:00+00:00'
    elif change == 'oversize': receipt.cases[0].stderr = 'x'*65536
    elif change == 'duplicate_container': receipt.cases[1].container_id = receipt.cases[0].container_id
    passed, checks = verify_python(call, receipt, receipt.model_copy(deep=True), receipt.job_id, '0'*64, config_hash(call.runtime),
                                   [case.deadline_receipt for case in receipt.cases])
    assert not passed
