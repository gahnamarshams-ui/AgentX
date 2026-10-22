"""Real SQLite and filesystem integration; only model responses are mocked."""
import asyncio
import copy
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from conftest import ProviderResponse as SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import ai
from app.config import Settings
from app.database import connection
from app.execution.agents import OutputVerifier, PolicySafety, WorkspaceExecutor, json_checks
from app.execution.models import ExecutionPlan, ToolCall, ToolResult, Verification
from app.execution.security import ExecutionError, ToolFailure
from app.execution.store import recover
from app.execution.workspace import Workspace, safe_path
from app.main import create_app
from app.planning import recover_expired


def plan(write=True):
    calls = [dict(name='write_text', path='output.json', content='{"answer":42}', required_keys=[])] if write else []
    calls += [dict(name='validate_json', path='output.json', content=None, required_keys=['answer'])]
    return ExecutionPlan.model_validate(dict(goal='Create and validate a JSON document' if write else 'Validate a JSON document',
        assumptions=[], constraints=[], clarification_needed=False, clarification_questions=[], steps=[
            dict(number=i, action=call['name'], purpose='Produce checked workspace evidence', expected_output='A valid JSON document',
                 dependencies=[i-1] if i > 1 else [], tool=call) for i, call in enumerate(calls, 1)]))


@pytest.fixture
def settings(tmp_path):
    from app.execution.workspace import workspace_supported
    if not workspace_supported(): pytest.skip('Safe handle-relative workspace access unavailable on this host')
    # Test fixtures explicitly provision their disposable workspace, not the app.
    (tmp_path / 'workspace').mkdir(mode=0o700)
    return Settings(_env_file=None, database_path=tmp_path / 'tasks.sqlite3', workspace_path=tmp_path / 'workspace',
                    execution_enabled=True, openai_api_key='test-placeholder-only')


@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=SimpleNamespace(status='completed', output_parsed=plan()))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=client)
    manager.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=manager)
    monkeypatch.setattr(ai, 'AsyncOpenAI', factory)
    return client, factory


def local_client(settings, **kwargs):
    return TestClient(create_app(settings), base_url='http://127.0.0.1', client=('127.0.0.1', 50000), **kwargs)


@pytest.fixture
def client(settings, sdk):
    with local_client(settings) as client:
        yield client


def create(client, description='Create output.json containing answer 42 and validate it'):
    result = client.post('/api/tasks', json={'description': description})
    assert result.status_code == 201
    return '/api/tasks/' + result.json()['id']


def approve(client, url, run, decision='approve'):
    return client.post(url + '/approval', json={'plan_digest': run['plan_digest'], 'decision': decision})


def workspace_snapshot(root):
    """Include directory/file identity and metadata, not merely output existence."""
    return {
        str(path.relative_to(root)): (
            path.stat().st_ino, path.stat().st_mode, path.stat().st_mtime_ns,
            path.stat().st_ctime_ns, path.read_bytes() if path.is_file() else None,
        ) for path in [root, *root.rglob('*')]
    }


def test_missing_workspace_startup_does_not_create_directories(settings, tmp_path):
    missing = tmp_path / 'unprovisioned' / 'workspace'
    settings.workspace_path = missing
    with pytest.raises(OSError):
        Workspace(settings)
    assert not missing.parent.exists()
    with pytest.raises(RuntimeError, match='existing ordinary directory'):
        with local_client(settings):
            pytest.fail('Execution startup must fail closed without a provisioned workspace')
    assert not missing.parent.exists()


@pytest.mark.parametrize('decision', ['none', 'missing', 'mismatch', 'stale', 'reject'])
def test_preparation_review_and_invalid_approval_never_mutate_workspace(settings, sdk, monkeypatch, decision):
    (settings.workspace_path / 'existing.txt').write_text('Preserve existing data')
    before = workspace_snapshot(settings.workspace_path)
    with local_client(settings) as c:
        execute = MagicMock(side_effect=AssertionError('No tool may run'))
        monkeypatch.setattr(c.app.state.orchestrator.executor, 'execute', execute)
        url = create(c)
        prepared = c.post(url + '/orchestrate')
        assert prepared.status_code == 200, prepared.text
        run = prepared.json()
        assert run['phase'] == 'awaiting_approval'
        for suffix in ['', '/execution', '/activity', '/history']:
            assert c.get(url + suffix).status_code == 200
        if decision == 'missing':
            assert c.post(url + '/approval', json={'decision': 'approve'}).status_code == 422
        elif decision == 'mismatch':
            assert approve(c, url, {'plan_digest': '0' * 64}).status_code == 409
        elif decision == 'stale':
            with connection(settings.database_path) as db:
                changed = plan().model_dump()
                changed['steps'][0]['tool']['content'] = '{"answer":43}'
                db.execute('UPDATE execution_runs SET plan_json=? WHERE task_id=?',
                           (ExecutionPlan.model_validate(changed).model_dump_json(), run['task_id']))
            assert approve(c, url, run).status_code == 409
        elif decision == 'reject':
            assert approve(c, url, run, 'reject').json()['phase'] == 'cancelled'
        execute.assert_not_called()
        assert workspace_snapshot(settings.workspace_path) == before
        with connection(settings.database_path) as db:
            assert db.execute('SELECT sum(attempts) FROM execution_steps WHERE task_id=?',
                              (run['task_id'],)).fetchone()[0] == 0
            assert not db.execute("SELECT 1 FROM execution_approvals WHERE task_id=? AND decision='approve'",
                                  (run['task_id'],)).fetchone()
    assert workspace_snapshot(settings.workspace_path) == before


@pytest.mark.parametrize('mode', ['navigate', 'no-cors'])
def test_execution_rejects_browser_simple_request_modes(client, settings, monkeypatch, mode):
    url = create(client)
    before = workspace_snapshot(settings.workspace_path)
    owner = client.app.state.orchestrator
    start, approve_mock, execute = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(owner, 'start', start)
    monkeypatch.setattr(owner, 'approve', approve_mock)
    monkeypatch.setattr(owner, 'execute', execute)
    headers = {'Sec-Fetch-Mode': mode}
    assert client.post(url + '/orchestrate', headers=headers).status_code == 403
    assert client.post(url + '/approval', headers=headers,
                       json={'plan_digest': '0' * 64, 'decision': 'approve'}).status_code == 403
    assert client.get(url + '/execution', headers=headers).status_code == 403
    start.assert_not_called(); approve_mock.assert_not_called(); execute.assert_not_called()
    assert workspace_snapshot(settings.workspace_path) == before


@pytest.mark.parametrize('change', ['disabled', 'task_status'])
def test_approval_rechecks_internal_configuration_and_task_state(client, settings, change):
    from app.execution.models import ApprovalRequest
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    before = workspace_snapshot(settings.workspace_path)
    if change == 'disabled':
        settings.execution_enabled = False  # This isolated fixture only; never server configuration.
    else:
        with connection(settings.database_path) as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (run['task_id'],))
    with pytest.raises(ExecutionError):
        client.app.state.orchestrator.approve(run['task_id'], ApprovalRequest(plan_digest=run['plan_digest'], decision='approve'))
    with connection(settings.database_path) as db:
        assert not db.execute('SELECT 1 FROM execution_approvals WHERE task_id=?', (run['task_id'],)).fetchone()
    assert workspace_snapshot(settings.workspace_path) == before


@pytest.mark.parametrize('change', ['disabled', 'approval_flag', 'task_status'])
def test_tool_authorization_rechecks_configuration_and_saved_state(client, settings, monkeypatch, change):
    from app.execution.models import ApprovalRequest
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    orchestrator = client.app.state.orchestrator
    owner = orchestrator.approve(run['task_id'], ApprovalRequest(plan_digest=run['plan_digest'], decision='approve'))
    before = workspace_snapshot(settings.workspace_path)
    execute = MagicMock(side_effect=AssertionError('Tool must not run after authorization changes'))
    monkeypatch.setattr(orchestrator.executor, 'execute', execute)
    if change == 'disabled':
        settings.execution_enabled = False
    else:
        with connection(settings.database_path) as db:
            if change == 'approval_flag':
                db.execute('UPDATE execution_runs SET approval_required=0 WHERE task_id=?', (run['task_id'],))
            else:
                db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (run['task_id'],))
    with pytest.raises(ExecutionError):
        orchestrator.execute(run['task_id'], owner)
    execute.assert_not_called()
    assert workspace_snapshot(settings.workspace_path) == before


def test_write_approval_verification_and_restart(client, settings, sdk):
    url = create(client)
    response = client.post(url + '/orchestrate')
    assert response.status_code == 200, response.text
    run = response.json()
    assert run['phase'] == 'awaiting_approval' and run['approval_required']
    assert not (settings.workspace_path / 'output.json').exists()
    task = client.get(url).json()
    assert task['final_result'] is None and 'tool' not in task['plan']['steps'][0]
    assert client.post(url + '/plan').status_code == 409
    with local_client(settings) as restarted:
        assert restarted.get(url + '/execution').json() == run
        finished = approve(restarted, url, run)
        assert finished.status_code == 200, finished.text
        assert finished.json()['phase'] == 'completed'
        assert [s['status'] for s in finished.json()['steps']] == ['verified', 'verified']
        assert 'json_checks_reexecuted' in finished.json()['steps'][1]['verification']['checks']
        assert restarted.get(url).json()['status'] == 'completed'
        assert 'Verified final deliverable' in restarted.get(url).json()['final_result']
        assert approve(restarted, url, run).status_code == 409
    assert (settings.workspace_path / 'output.json').read_text() == '{"answer":42}'
    events = client.get(url + '/history').json()['events']
    assert [e['created_at'] for e in events] == sorted(e['created_at'] for e in events)
    messages = ' '.join(e['message'] for e in events)
    assert all(f'[{agent}]' in messages for agent in ['Planner', 'Safety', 'Executor', 'Verifier', 'Orchestrator'])
    assert events[-1]['status'] == 'completed'
    assert sdk[0].responses.create.call_args.kwargs['text']['format']['name'] == 'ExecutionPlan'
    assert sdk[0].responses.create.call_args.kwargs['store'] is False
    assert sdk[0].responses.create.await_count == 1


def test_read_only_requires_approval_then_executes_and_verifies(client, settings, sdk):
    (settings.workspace_path / 'output.json').write_text('{"answer": 42}')
    sdk[0].responses.create.return_value.output_parsed = plan(False)
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    assert run['phase'] == 'awaiting_approval' and run['approval_required']
    assert run['approval_decision'] is None and run['steps'][0]['attempts'] == 0
    run = approve(client, url, run).json()
    assert run['phase'] == 'completed' and run['steps'][0]['verification']['passed']
    assert run['approval_decision'] == 'approve' and run['steps'][0]['attempts'] == 1
    assert client.post(url + '/orchestrate').status_code == 409


def test_reject_and_wrong_digest(client, settings):
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    assert approve(client, url, {'plan_digest': '0'*64}).status_code == 409
    assert client.post(url + '/approval', json={'decision':'approve'}).status_code == 422
    assert approve(client, url, run, 'reject').json()['phase'] == 'cancelled'
    assert not (settings.workspace_path / 'output.json').exists()
    assert not client.get(url).json()['final_result'] or 'NOT VERIFIED' in client.get(url).json()['final_result']


def test_missing_key_unknown_invalid_state(client, settings, sdk):
    settings.openai_api_key = None
    url = create(client)
    assert client.post(url + '/orchestrate').status_code == 503
    assert client.get(url).json()['status'] == 'pending'
    assert client.post(f'/api/tasks/{uuid4()}/orchestrate').status_code == 404
    assert client.get(f'/api/tasks/{uuid4()}/execution').status_code == 404
    assert client.get(url + '/execution').status_code == 404
    assert client.post('/api/tasks/not-uuid/orchestrate').status_code == 422
    sdk[1].assert_not_called()


@pytest.mark.parametrize('mode', ['disabled', 'remote', 'host', 'origin', 'fetch'])
def test_execution_guard(settings, mode):
    if mode == 'disabled': settings.execution_enabled = False
    app = create_app(settings)
    with TestClient(app, base_url='http://evil.example' if mode == 'host' else 'http://127.0.0.1',
                    client=('203.0.113.5' if mode == 'remote' else '127.0.0.1', 50000)) as c:
        headers = {'Origin': 'https://evil.example'} if mode == 'origin' else {'Sec-Fetch-Site': 'cross-site'} if mode == 'fetch' else {}
        assert c.post(f'/api/tasks/{uuid4()}/orchestrate', headers=headers).status_code == (503 if mode == 'disabled' else 403)


@pytest.mark.parametrize('kind', ['timeout', 'malformed', 'provider'])
def test_ai_failures_safe_and_persisted(client, sdk, kind):
    if kind == 'timeout': sdk[0].responses.create.side_effect = TimeoutError('private provider detail')
    elif kind == 'provider': sdk[0].responses.create.side_effect = RuntimeError('private provider detail')
    else: sdk[0].responses.create.return_value.output_parsed = None
    url = create(client)
    result = client.post(url + '/orchestrate')
    assert result.status_code in {502, 504}
    assert 'private provider detail' not in result.text
    assert client.get(url).json()['status'] == 'failed'
    assert client.get(url).json()['plan'] is None
    assert client.get(url + '/execution').json()['phase'] == 'failed'
    assert client.post(url + '/plan').status_code == 409


def test_concurrent_planning_claim(client, sdk):
    entered, release = threading.Event(), threading.Event()
    async def slow(**kwargs):
        entered.set()
        await asyncio.to_thread(release.wait, 3)
        return SimpleNamespace(status='completed', output_parsed=plan())
    sdk[0].responses.create.side_effect = slow
    url = create(client)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(client.post, url + '/orchestrate')
        assert entered.wait(2)
        try:
            assert client.post(url + '/orchestrate').status_code == 409
            assert client.get(url + '/execution').json()['phase'] == 'planning'
        finally: release.set()
        assert first.result().status_code == 200
    assert sdk[0].responses.create.await_count == 1


def test_concurrent_approval_only_one_owner(client, settings, monkeypatch):
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    orchestrator = client.app.state.orchestrator
    execute = orchestrator.execute
    entered, release = threading.Event(), threading.Event()
    def paused(*args):
        entered.set()
        assert release.wait(3)
        execute(*args)
    monkeypatch.setattr(orchestrator, 'execute', paused)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(approve, client, url, run)
        assert entered.wait(2)
        try:
            assert approve(client, url, run).status_code == 409
            assert client.get(url + '/execution').json()['phase'] == 'executing'
            assert client.get(url).json()['status'] == 'running'
        finally: release.set()
        assert first.result().json()['phase'] == 'completed'
    assert (settings.workspace_path / 'output.json').exists()


@pytest.mark.parametrize('transient', [True, False])
def test_bounded_read_retries(client, settings, sdk, monkeypatch, transient):
    sdk[0].responses.create.return_value.output_parsed = plan(False)
    executor = client.app.state.orchestrator.executor
    execute = MagicMock(side_effect=ToolFailure('private contents', retryable=transient))
    monkeypatch.setattr(executor, 'execute', execute)
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    execute.assert_not_called()
    run = approve(client, url, run).json()
    assert run['phase'] == 'failed'
    assert run['steps'][0]['attempts'] == (settings.execution_max_attempts if transient else 1)
    assert 'private contents' not in str(run)
    assert not client.get(url).json()['final_result'] or 'NOT VERIFIED' in client.get(url).json()['final_result']


def test_read_retry_success(client, settings, sdk, monkeypatch):
    (settings.workspace_path / 'output.json').write_text('{"answer":42}')
    sdk[0].responses.create.return_value.output_parsed = plan(False)
    executor = client.app.state.orchestrator.executor
    actual = executor.execute(plan(False).steps[0].tool)
    monkeypatch.setattr(executor, 'execute', MagicMock(side_effect=[ToolFailure('busy', retryable=True), actual]))
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    run = approve(client, url, run).json()
    assert run['phase'] == 'completed' and run['steps'][0]['attempts'] == 2
    assert run['steps'][0]['error'] is None


def test_write_failure_never_retried_or_dependents_run(client, monkeypatch):
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    execute = MagicMock(side_effect=ToolFailure('ambiguous write', retryable=True))
    monkeypatch.setattr(client.app.state.orchestrator.executor, 'execute', execute)
    result = approve(client, url, run).json()
    assert result['phase'] == 'failed'
    assert [s['attempts'] for s in result['steps']] == [1, 0]
    execute.assert_called_once()


def test_verifier_detects_fabricated_output(client, settings, sdk, monkeypatch):
    (settings.workspace_path / 'output.json').write_text('{"answer":42}')
    sdk[0].responses.create.return_value.output_parsed = plan(False)
    fake = ToolResult(path='output.json', sha256='0'*64, byte_count=13, checks=['valid_json','required_keys_present'])
    monkeypatch.setattr(client.app.state.orchestrator.executor, 'execute', MagicMock(return_value=fake))
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    run = approve(client, url, run).json()
    assert run['phase'] == 'failed' and not run['steps'][0]['verification']['passed']
    assert not client.get(url).json()['final_result'] or 'NOT VERIFIED' in client.get(url).json()['final_result']


def test_plan_tampering_prevents_approval(client, settings):
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    with connection(settings.database_path) as db:
        db.execute("UPDATE execution_steps SET dependencies='[1]' WHERE number=1")
    assert approve(client, url, run).status_code == 409
    assert not (settings.workspace_path / 'output.json').exists()


def test_expired_execution_fails_without_replay(client, settings):
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    from app.execution.models import ApprovalRequest
    orchestrator = client.app.state.orchestrator
    owner = orchestrator.approve(url.split('/')[-1], ApprovalRequest(plan_digest=run['plan_digest'], decision='approve'))
    with connection(settings.database_path) as db:
        db.execute("UPDATE execution_runs SET expires_at='2000-01-01' WHERE task_id=?", (url.split('/')[-1],))
    recover(settings.database_path)
    recover(settings.database_path)
    assert client.get(url + '/execution').json()['phase'] == 'failed'
    with pytest.raises(ExecutionError): orchestrator.execute(url.split('/')[-1], owner)
    assert not (settings.workspace_path / 'output.json').exists()


def test_expired_planning_recovers(client, settings):
    url = create(client)
    client.app.state.orchestrator.begin(url.split('/')[-1])
    with connection(settings.database_path) as db:
        db.execute("UPDATE planning_attempts SET expires_at='2000-01-01'")
    recover_expired(settings.database_path)
    recover(settings.database_path)
    assert client.get(url).json()['status'] == 'failed'
    assert client.get(url + '/execution').json()['phase'] == 'failed'


def test_plan_transaction_rollback(client, settings):
    url = create(client)
    with connection(settings.database_path) as db:
        db.execute("CREATE TRIGGER fail_steps BEFORE INSERT ON execution_steps BEGIN SELECT RAISE(ABORT, 'private storage detail'); END")
    result = client.post(url + '/orchestrate')
    assert result.status_code == 503 and 'private storage detail' not in result.text
    assert client.get(url).json()['plan'] is None
    assert client.get(url + '/execution').json()['steps'] == []
    assert client.get(url).json()['status'] == 'failed'


def test_post_write_database_failure_is_not_replayed(client, settings):
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    with connection(settings.database_path) as db:
        db.execute("CREATE TRIGGER fail_result BEFORE UPDATE OF result ON execution_steps WHEN NEW.result IS NOT NULL BEGIN SELECT RAISE(ABORT, 'private storage detail'); END")
    result = approve(client, url, run)
    assert result.status_code == 503 and 'private storage detail' not in result.text
    assert (settings.workspace_path / 'output.json').exists()
    assert client.get(url).json()['status'] == 'failed'
    assert not client.get(url).json()['final_result'] or 'NOT VERIFIED' in client.get(url).json()['final_result']
    assert approve(client, url, run).status_code == 409


@pytest.mark.parametrize('path', ['../out.txt', '/tmp/out.txt', '.env', 'a/../out.txt', 'a//out.txt', 'a\\out.txt', 'secret.txt', 'a.py', '.hidden/out.txt'])
def test_workspace_rejects_unsafe_paths(path):
    with pytest.raises(ExecutionError): safe_path(path)


@pytest.mark.parametrize('kind', ['symlink', 'parent_symlink', 'hardlink', 'fifo', 'oversize', 'binary', 'secret'])
def test_workspace_rejects_unsafe_files(settings, tmp_path, kind):
    workspace = Workspace(settings)
    target = settings.workspace_path / 'input.txt'
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside')
    path = 'input.txt'
    if kind == 'symlink':
        try: target.symlink_to(outside)
        except OSError as exc:
            if os.name == 'nt' and exc.winerror == 1314: pytest.skip('Windows symlink privilege unavailable; junction tests still run')
            raise
    elif kind == 'parent_symlink':
        try: (settings.workspace_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
        except OSError as exc:
            if os.name == 'nt' and exc.winerror == 1314: pytest.skip('Windows symlink privilege unavailable; junction tests still run')
            raise
        path = 'alias/outside.txt'
    elif kind == 'hardlink': os.link(outside, target)
    elif kind == 'fifo':
        if not hasattr(os, 'mkfifo'): pytest.skip('FIFO creation is POSIX-specific')
        os.mkfifo(target)
    elif kind == 'oversize': target.write_bytes(b'a'*65537)
    elif kind == 'binary': target.write_bytes(b'\xff')
    else: target.write_text('password=never-disclose-this')
    with pytest.raises((ExecutionError, ToolFailure)): workspace.read(path)
    assert outside.read_text() == 'outside'


def test_exclusive_write_and_actual_json_tests(settings):
    workspace = Workspace(settings)
    workspace.create('data.json', '{"ok":true}')
    with pytest.raises(ToolFailure): workspace.create('data.json', 'overwrite')
    assert workspace.read('data.json') == b'{"ok":true}'
    for value in [b'bad', b'{"x":NaN}', b'{}']:
        with pytest.raises(ToolFailure): json_checks(value, ['ok'])
    call = ToolCall(name='validate_json', path='data.json', content=None, required_keys=['ok'])
    result = WorkspaceExecutor(workspace).execute(call)
    assert OutputVerifier(workspace).verify(call, result).passed
    result.checks = ['claimed success']
    assert not OutputVerifier(workspace).verify(call, result).passed


@pytest.mark.parametrize('change', ['tool', 'dependency', 'content', 'length', 'count'])
def test_execution_schema_validation(change):
    value = plan().model_dump()
    if change == 'tool': value['steps'][0]['tool']['name'] = 'shell'
    if change == 'dependency': value['steps'][0]['dependencies'] = [2]
    if change == 'content': value['steps'][1]['tool']['content'] = 'invalid'
    if change == 'length': value['steps'][0]['tool']['content'] = 'x'*8001
    if change == 'count': value['steps'] *= 7
    with pytest.raises(ValidationError): ExecutionPlan.model_validate(value)


def test_policy_dependencies_clarification_and_secret(settings):
    value = plan().model_dump()
    value['steps'][1]['dependencies'] = []
    with pytest.raises(ExecutionError): PolicySafety(settings).assess(ExecutionPlan.model_validate(value))
    value = plan().model_dump()
    value['clarification_needed'] = True
    value['clarification_questions'] = ['What should the file contain?']
    with pytest.raises(ExecutionError): PolicySafety(settings).assess(ExecutionPlan.model_validate(value))
    value = plan().model_dump()
    value['steps'][0]['tool']['content'] = settings.openai_api_key.get_secret_value()
    with pytest.raises(ExecutionError): PolicySafety(settings).assess(ExecutionPlan.model_validate(value))


def test_configured_secret_not_sent_to_model(client, settings, sdk):
    url = create(client, 'Use ' + settings.openai_api_key.get_secret_value())
    result = client.post(url + '/orchestrate')
    assert result.status_code == 422
    assert settings.openai_api_key.get_secret_value() not in result.text
    sdk[1].assert_not_called()
    assert client.get(url + '/execution').json()['phase'] == 'failed'


def test_invalid_state_cannot_orchestrate(client, settings, sdk):
    url = create(client)
    with connection(settings.database_path) as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (url.split('/')[-1],))
    assert client.post(url + '/orchestrate').status_code == 409
    sdk[1].assert_not_called()


def test_active_lease_not_recovered_and_missing_approval_blocks_tools(client, settings):
    from app.execution.models import ApprovalRequest
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    orchestrator = client.app.state.orchestrator
    task_id = url.split('/')[-1]
    owner = orchestrator.approve(task_id, ApprovalRequest(plan_digest=run['plan_digest'], decision='approve'))
    recover(settings.database_path)
    assert client.get(url + '/execution').json()['phase'] == 'executing'
    with connection(settings.database_path) as db:
        db.execute('DELETE FROM execution_approvals WHERE task_id=?', (task_id,))
    with pytest.raises(ExecutionError): orchestrator.execute(task_id, owner)
    assert not (settings.workspace_path / 'output.json').exists()
    assert client.get(url).json()['status'] == 'failed'


def test_changed_file_fails_independent_verification(settings):
    workspace = Workspace(settings)
    call = plan().steps[0].tool
    result = WorkspaceExecutor(workspace).execute(call)
    (settings.workspace_path / call.path).write_text('{"answer":0}')
    assert not OutputVerifier(workspace).verify(call, result).passed


def test_secret_model_output_not_persisted(client, sdk, settings):
    value = plan().model_dump()
    value['steps'][0]['tool']['content'] = settings.openai_api_key.get_secret_value()
    sdk[0].responses.create.return_value.output_parsed = ExecutionPlan.model_validate(value)
    url = create(client)
    result = client.post(url + '/orchestrate')
    assert result.status_code == 422
    for suffix in ['', '/execution', '/history']:
        assert settings.openai_api_key.get_secret_value() not in client.get(url + suffix).text
    assert client.get(url).json()['plan'] is None


def test_claim_history_failure_has_no_run_or_state_change(client, settings, sdk):
    url = create(client)
    with connection(settings.database_path) as db:
        db.execute("CREATE TRIGGER fail_claim BEFORE INSERT ON task_history BEGIN SELECT RAISE(ABORT, 'private storage detail'); END")
    assert client.post(url + '/orchestrate').status_code == 503
    assert client.get(url).json()['status'] == 'pending'
    assert client.get(url + '/execution').status_code == 404
    sdk[1].assert_not_called()


@pytest.mark.parametrize('destination', ['completed', 'verifying', 'planning'])
def test_state_machine_rejects_skipped_or_backward_transitions(client, settings, destination):
    from app.execution.store import transition
    url = create(client)
    client.post(url + '/orchestrate')
    before = client.get(url + '/history').json()
    with pytest.raises(ExecutionError):
        with connection(settings.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            transition(db, url.split('/')[-1], destination, 'Must never be recorded')
    assert client.get(url + '/execution').json()['phase'] == 'awaiting_approval'
    assert client.get(url + '/history').json() == before


def test_state_machine_requires_approval_and_verified_results(client, settings):
    from app.execution.store import transition
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    task_id = url.split('/')[-1]
    with pytest.raises(ExecutionError, match='approval'):
        with connection(settings.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            transition(db, task_id, 'executing', 'Must not execute')
    from app.execution.models import ApprovalRequest
    client.app.state.orchestrator.approve(task_id, ApprovalRequest(plan_digest=run['plan_digest'], decision='approve'))
    with connection(settings.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        transition(db, task_id, 'verifying', 'Checking completion guard')
    with pytest.raises(ExecutionError, match='verified output'):
        with connection(settings.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            transition(db, task_id, 'completed', 'Must not complete')
    assert not client.get(url).json()['final_result'] or 'NOT VERIFIED' in client.get(url).json()['final_result']


def test_executor_registry_is_explicit_and_revalidates_contract(settings):
    executor = WorkspaceExecutor(Workspace(settings))
    assert set(executor.tools) == {'read_text', 'write_text', 'validate_json'}
    with pytest.raises(TypeError): executor.tools['shell'] = lambda call: None
    # Bypass model construction deliberately; the execution boundary must revalidate.
    forged = ToolCall.model_construct(name='shell', path='out.txt', content=None, required_keys=[])
    with pytest.raises(ValidationError): executor.execute(forged)


@pytest.mark.parametrize('tool', ['read_text', 'validate_json', 'write_text'])
def test_preparation_never_calls_tools_and_persists_digest(client, settings, sdk, monkeypatch, tool):
    proposal = plan().model_dump()
    call = dict(name=tool, path='output.json', content='{"answer":42}' if tool == 'write_text' else None,
                required_keys=['answer'] if tool == 'validate_json' else [])
    proposal['steps'] = [dict(number=1, action=tool, purpose='Check approval',
                              expected_output='Workspace evidence', dependencies=[], tool=call)]
    proposal = ExecutionPlan.model_validate(proposal)
    sdk[0].responses.create.return_value.output_parsed = proposal
    orchestrator = client.app.state.orchestrator
    execute, verify = MagicMock(), MagicMock()
    monkeypatch.setattr(orchestrator.executor, 'execute', execute)
    monkeypatch.setattr(orchestrator.verifier, 'verify', verify)
    # A false safety return cannot enable automatic execution.
    monkeypatch.setattr(orchestrator.safety, 'assess', MagicMock(return_value=False))
    url = create(client)
    result = client.post(url + '/orchestrate')
    assert result.status_code == 200
    run = result.json()
    from app.execution.store import digest
    assert run['plan_digest'] == digest(proposal, orchestrator.workspace.identity)
    assert run['phase'] == 'awaiting_approval' and run['approval_required']
    assert run['steps'][0]['status'] == 'pending' and run['steps'][0]['attempts'] == 0
    assert run['steps'][0]['result'] is None and run['steps'][0]['verification'] is None
    assert client.get(url + '/execution').json() == run
    execute.assert_not_called()
    verify.assert_not_called()
    assert not (settings.workspace_path / 'output.json').exists()


@pytest.mark.parametrize('invalid', ['missing', 'mismatch', 'stale', 'rejected'])
def test_read_only_invalid_approval_never_runs_tools(client, settings, sdk, monkeypatch, invalid):
    sdk[0].responses.create.return_value.output_parsed = plan(False)
    execute = MagicMock()
    monkeypatch.setattr(client.app.state.orchestrator.executor, 'execute', execute)
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    before = client.get(url + '/history').json()
    if invalid == 'missing':
        assert client.post(url + '/approval', json={'decision': 'approve'}).status_code == 422
    elif invalid == 'mismatch':
        assert approve(client, url, {'plan_digest': '0'*64}).status_code == 409
    elif invalid == 'stale':
        with connection(settings.database_path) as db:
            changed = plan(False).model_dump()
            changed['steps'][0]['tool']['path'] = 'changed.json'
            db.execute('UPDATE execution_runs SET plan_json=? WHERE task_id=?',
                       (ExecutionPlan.model_validate(changed).model_dump_json(), run['task_id']))
        assert approve(client, url, run).status_code == 409
    else:
        assert approve(client, url, run, 'reject').json()['phase'] == 'cancelled'
        assert approve(client, url, run).status_code == 409
    if invalid != 'rejected':
        assert client.get(url + '/history').json() == before
    execute.assert_not_called()
    with connection(settings.database_path) as db:
        assert not db.execute("SELECT 1 FROM execution_approvals WHERE task_id=? AND decision='approve'",
                              (run['task_id'],)).fetchone()


def test_read_only_cannot_bypass_gate_via_state_or_executor(client, settings, sdk, monkeypatch):
    from app.execution.store import transition, expiry
    sdk[0].responses.create.return_value.output_parsed = plan(False)
    url = create(client)
    run = client.post(url + '/orchestrate').json()
    orchestrator = client.app.state.orchestrator
    execute = MagicMock()
    monkeypatch.setattr(orchestrator.executor, 'execute', execute)
    with connection(settings.database_path) as db:
        db.execute('UPDATE execution_runs SET approval_required=0 WHERE task_id=?', (run['task_id'],))
    with pytest.raises(ExecutionError, match='approval'):
        with connection(settings.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            transition(db, run['task_id'], 'executing', 'Must fail')
    # Legacy executing read-only runs still need approval evidence.
    with connection(settings.database_path) as db:
        db.execute("UPDATE execution_runs SET phase='executing',expires_at=? WHERE task_id=?",
                   (expiry(120), run['task_id']))
        owner = db.execute('SELECT owner FROM execution_runs WHERE task_id=?', (run['task_id'],)).fetchone()['owner']
    with pytest.raises(ExecutionError):
        orchestrator.execute(run['task_id'], owner)
    execute.assert_not_called()
    assert client.get(url + '/execution').json()['phase'] == 'failed'


def five_plan(required_key='answer'):
    data = plan().model_dump()
    data['workflow'] = 'five_agent'
    calls = [dict(name='read_text', path='source.json', content=None, required_keys=[]),
             dict(name='write_text', path='output.json', content='{"answer":42}', required_keys=[]),
             dict(name='validate_json', path='output.json', content=None, required_keys=[required_key])]
    data['steps'] = [dict(number=i, action=call['name'], purpose='Evidence-based JSON demo',
                         expected_output='Verified JSON evidence', dependencies=[i-1] if i > 1 else [], tool=call)
                     for i, call in enumerate(calls, 1)]
    return ExecutionPlan.model_validate(data)


def test_five_roles_real_tools_order_handoffs_and_persistence(client, settings, sdk, monkeypatch):
    from app.execution.agents import WorkspaceExecutor, OutputVerifier
    sdk[0].responses.create.return_value.output_parsed = five_plan()
    (settings.workspace_path / 'source.json').write_text('{"answer":42}')
    called, inputs = [], {}
    for cls, method, name in [(WorkspaceExecutor, 'research', 'Research'), (WorkspaceExecutor, 'implement', 'Developer'),
                              (WorkspaceExecutor, 'test_json', 'Testing'), (OutputVerifier, 'review', 'Review')]:
        original = getattr(cls, method)
        def wrapper(self, *args, _original=original, _name=name):
            called.append(_name)
            inputs[_name] = args[2] if _name == 'Review' else args[1]
            return _original(self, *args)
        monkeypatch.setattr(cls, method, wrapper)
    url = create(client, 'Five-agent JSON demo from existing source.json into new output.json')
    run = client.post(url + '/orchestrate').json()
    assert called == []
    assert run['phase'] == 'awaiting_approval'
    assert [a['status'] for a in run['agents']] == ['running', 'succeeded', 'running', 'pending', 'pending']
    assert not (settings.workspace_path / 'output.json').exists()
    assert approve(client, url, {'plan_digest': '0'*64}).status_code == 409
    assert client.post(url + '/approval', json={'decision': 'approve'}).status_code == 422
    assert called == []
    final = approve(client, url, run)
    assert final.status_code == 200
    finished = final.json()
    assert finished['phase'] == 'completed'
    assert called == ['Research', 'Developer', 'Testing', 'Review']
    assert inputs['Research'] == {}
    assert inputs['Developer'][1].content == '{"answer":42}'
    assert inputs['Testing'][2].sha256 == finished['steps'][1]['result']['sha256']
    assert set(inputs['Review']) == {1, 2, 3}
    assert [a['role'] for a in finished['agents']] == ['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier']
    assert all(a['status'] == 'succeeded' for a in finished['agents'])
    assert all(s['verification']['passed'] for s in finished['steps'])
    assert 'implementation_matches_research' in finished['agents'][-1]['checks']
    assert 'safety_policy_rechecked' in finished['agents'][-1]['checks']
    assert 'json_checks_reexecuted' in finished['agents'][-1]['checks']
    assert (settings.workspace_path / 'output.json').read_bytes() == b'{"answer":42}'
    assert 'Verified final deliverable' in client.get(url).json()['final_result']
    messages = ' '.join(e['message'] for e in client.get(url + '/history').json()['events'])
    for role in ['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier']: assert '[' + role in messages
    with local_client(settings) as restarted:
        assert restarted.get(url + '/execution').json() == finished
        assert approve(restarted, url, run).status_code == 409
    assert sdk[0].responses.create.await_count == 1


@pytest.mark.parametrize('failure', ['Research', 'Developer', 'Testing', 'Review'])
def test_five_roles_failures_never_claim_success(client, settings, sdk, monkeypatch, failure):
    from app.execution.agents import OutputVerifier
    sdk[0].responses.create.return_value.output_parsed = five_plan('missing_key' if failure == 'Testing' else 'answer')
    if failure != 'Research':
        (settings.workspace_path / 'source.json').write_text('{"answer":0}' if failure == 'Developer' else '{"answer":42}')
    if failure == 'Review':
        original = OutputVerifier.review
        def altered(self, safety, plan, results):
            (settings.workspace_path / 'output.json').write_text('{"answer":0}')
            return original(self, safety, plan, results)
        monkeypatch.setattr(OutputVerifier, 'review', altered)
    url = create(client)
    pending = client.post(url + '/orchestrate').json()
    approve(client, url, pending)
    final = client.get(url + '/execution').json()
    assert final['phase'] == 'failed'
    by_role = {a['role']: a for a in final['agents']}
    assert by_role['Verifier' if failure == 'Review' else 'Executor']['status'] == 'failed'
    assert not client.get(url).json()['final_result'] or 'NOT VERIFIED' in client.get(url).json()['final_result']
    if failure != 'Review': assert by_role['Verifier']['status'] == 'skipped'
    if failure in {'Research', 'Developer'}:
        assert not (settings.workspace_path / 'output.json').exists()
        assert final['steps'][2]['attempts'] == 0
    if failure == 'Testing':
        assert by_role['Executor']['attempts'] == settings.execution_max_attempts
        messages = [e['message'] for e in client.get(url + '/history').json()['events']]
        assert any('[Executor]' in m and 'bounded retry scheduled' in m for m in messages)
        assert any('[Executor]' in m and 'failed.' in m for m in messages)


def test_five_workflow_source_tampering_stops_development(client, settings, sdk, monkeypatch):
    from app.execution.agents import WorkspaceExecutor
    sdk[0].responses.create.return_value.output_parsed = five_plan()
    (settings.workspace_path / 'source.json').write_text('{"answer":42}')
    original = WorkspaceExecutor.implement
    def changed(self, *args):
        (settings.workspace_path / 'source.json').write_text('{"answer":43}')
        return original(self, *args)
    monkeypatch.setattr(WorkspaceExecutor, 'implement', changed)
    url = create(client)
    pending = client.post(url + '/orchestrate').json()
    assert approve(client, url, pending).json()['phase'] == 'failed'
    assert not (settings.workspace_path / 'output.json').exists()


@pytest.mark.parametrize('change', ['order', 'dependency', 'destination', 'keys'])
def test_five_workflow_contract_cannot_skip_roles(change):
    data = five_plan().model_dump()
    if change == 'order': data['steps'][0]['tool']['name'] = 'validate_json'
    if change == 'dependency': data['steps'][1]['dependencies'] = []
    if change == 'destination': data['steps'][2]['tool']['path'] = 'other.json'
    if change == 'keys': data['steps'][2]['tool']['required_keys'] = []
    with pytest.raises(ValidationError): ExecutionPlan.model_validate(data)


def test_five_plan_real_sdk_parser_with_mock_http(settings, monkeypatch):
    import json
    import httpx2
    from openai import AsyncOpenAI as RealAsyncOpenAI
    from app.execution.agents import OpenAIPlanner
    requests = []
    proposal = five_plan()
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={
            'id': 'resp_test', 'object': 'response', 'created_at': 1, 'status': 'completed',
            'model': 'gpt-4.1-mini', 'error': None, 'incomplete_details': None,
            'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': proposal.model_dump_json(), 'annotations': []}]}]})
    def factory(**kwargs):
        return RealAsyncOpenAI(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False))
    monkeypatch.setattr(ai, 'AsyncOpenAI', factory)
    result = asyncio.run(OpenAIPlanner(settings).plan('Five-agent JSON workflow from source.json to output.json. Expected JSON {"answer":42}.'))
    assert result == proposal
    schema = requests[0]['text']['format']['schema']
    assert requests[0]['text']['format']['strict'] is True
    assert 'workflow' in schema['required']
    assert 'default' not in schema['properties']['workflow']


def test_research_json_equality_does_not_confuse_boolean_and_number():
    from app.execution.agents import same_json
    assert not same_json('{"answer":true}', '{"answer":1}')
    assert same_json('{ "answer":42 }', '{"answer":42}')


@pytest.mark.parametrize('workflow', ['workspace', 'five_agent'])
def test_required_five_roles_connected_for_every_workflow(client, settings, sdk, monkeypatch, workflow):
    from app.execution.orchestrator import Orchestrator
    from app.execution.agents import OpenAIPlanner
    proposed = five_plan() if workflow == 'five_agent' else plan()
    sdk[0].responses.create.return_value.output_parsed = proposed
    if workflow == 'five_agent':
        (settings.workspace_path / 'source.json').write_text('{"answer":42}')
    calls = []
    for cls, method, role in [(Orchestrator, 'begin', 'Orchestrator'),
                              (PolicySafety, 'assess', 'Safety'),
                              (WorkspaceExecutor, 'execute', 'Executor'),
                              (OutputVerifier, 'verify', 'Verifier')]:
        original = getattr(cls, method)
        def record(self, *args, _original=original, _role=role):
            calls.append(_role)
            return _original(self, *args)
        monkeypatch.setattr(cls, method, record)
    original_plan = OpenAIPlanner.plan
    async def record_plan(self, *args):
        calls.append('Planner')
        return await original_plan(self, *args)
    monkeypatch.setattr(OpenAIPlanner, 'plan', record_plan)
    url = create(client)
    pending = client.post(url + '/orchestrate').json()
    roles = ['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier']
    assert [a['role'] for a in pending['agents']] == roles
    assert calls == ['Orchestrator', 'Planner', 'Safety']
    assert all(s['attempts'] == 0 and s['result'] is None for s in pending['steps'])
    assert pending['approval_required'] and pending['phase'] == 'awaiting_approval'
    finished = approve(client, url, pending).json()
    assert finished['phase'] == 'completed'
    assert set(calls) == set(roles)
    assert [a['role'] for a in finished['agents']] == roles
    assert all(a['status'] == 'succeeded' for a in finished['agents'])
    assert finished['plan_digest'] == pending['plan_digest']
    assert client.get(url + '/execution').json() == finished


def test_conversation_deletion_cleans_pending_execution_records(client, settings):
    url = create(client)
    pending = client.post(url + '/orchestrate').json()
    # Read-only activity is available in disabled mode, with the same persisted evidence.
    disabled = settings.model_copy(update={'execution_enabled': False})
    with local_client(disabled) as c:
        assert c.get(url + '/activity').json() == pending
        assert c.delete(url).status_code == 200
    with connection(settings.database_path) as db:
        for table in ('tasks', 'task_history', 'planning_attempts', 'execution_runs', 'execution_steps', 'execution_approvals'):
            field = 'id' if table == 'tasks' else 'task_id'
            assert db.execute(f'SELECT count(*) FROM {table} WHERE {field}=?', (pending['task_id'],)).fetchone()[0] == 0
    assert list(settings.workspace_path.iterdir()) == []
