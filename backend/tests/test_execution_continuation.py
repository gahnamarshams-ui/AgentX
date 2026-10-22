"""State/approval regressions and real I/O on supported Windows/POSIX hosts."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import initialize_database, connection
from app.execution.models import ExecutionPlan, ApprovalRequest
from app.execution.orchestrator import Orchestrator
from app.execution.agents import PolicySafety
from app.execution.security import ExecutionError
from app.execution.workspace import workspace_supported
from app.main import create_app
from app.schemas import TaskCreate
from app.tasks import create_task

FEST = 'Build a fully functional college tech fest management web app with event registration, participant management, scheduling, budget tracking within ₹25,000 and an admin dashboard. Plan, implement, test and verify the project.'


def proposal(question=None):
    return ExecutionPlan(goal='Create approved fest requirements JSON', assumptions=[], constraints=['Budget: INR 25000'],
        clarification_needed=bool(question), clarification_questions=[question] if question else [], steps=[
            dict(number=1, action='Create approved JSON', purpose='Save exact approved content', expected_output='JSON file', dependencies=[],
                 tool=dict(name='write_text', path='fest.json', content='{"budget":25000}', required_keys=[])),
            dict(number=2, action='Check JSON', purpose='Check required budget key', expected_output='JSON validation evidence', dependencies=[1],
                 tool=dict(name='validate_json', path='fest.json', content=None, required_keys=['budget']))])


@pytest.fixture
def state(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path/'state.db', workspace_path=tmp_path/'workspace',
                        openai_api_key='test-placeholder-only', execution_enabled=True)
    initialize_database(settings.database_path)
    # State-only harness deliberately has no filesystem executor. It cannot claim real execution.
    owner = object.__new__(Orchestrator)
    owner.settings, owner.path = settings, settings.database_path
    owner.workspace = SimpleNamespace(identity='state-only-no-tools')
    owner.safety = PolicySafety(settings)
    owner.planner = SimpleNamespace(plan=AsyncMock(side_effect=[proposal('Only bounded JSON tools are available. Supply an approved JSON-only scope?'), proposal()]))
    task = create_task(settings.database_path, TaskCreate(description=FEST))
    return settings, owner, str(task.id)


def test_clarification_continues_same_task_preserves_goal_and_requires_new_approval(state):
    settings, owner, task_id = state
    first = asyncio.run(owner.start(task_id))
    assert first.phase == 'awaiting_user_input' and first.plan_digest is None and first.steps == []
    with pytest.raises(ExecutionError): owner.approve(task_id, ApprovalRequest(plan_digest='0'*64, decision='approve'))
    second = asyncio.run(owner.start(task_id, answer='Limit this task to fest.json with budget 25000 and validate its budget key.'))
    assert second.task_id == task_id and second.phase == 'awaiting_approval'
    assert all(s.attempts == 0 and s.result is None for s in second.steps)
    payload = json.loads(owner.planner.plan.call_args.args[0])
    assert payload['original_request'] == FEST and '25000' in payload['clarification_answers'][0]
    with connection(settings.database_path) as db:
        assert db.execute('SELECT description FROM tasks WHERE id=?', (task_id,)).fetchone()[0] == FEST
        assert db.execute('SELECT count(*) FROM execution_approvals').fetchone()[0] == 0
    assert owner.approve(task_id, ApprovalRequest(plan_digest=second.plan_digest, decision='reject')) is None


def test_conflicting_answer_cannot_produce_approval(state):
    _, owner, task_id = state
    owner.planner.plan.side_effect = [proposal(), proposal()]
    with connection(owner.path) as db:
        db.execute('UPDATE tasks SET description=? WHERE id=?', ('The format is exclusively JSON and exclusively YAML.', task_id))
    assert asyncio.run(owner.start(task_id)).phase == 'awaiting_user_input'
    again = asyncio.run(owner.start(task_id, answer='Also include budget 25000.'))
    assert again.phase == 'awaiting_user_input' and again.plan_digest is None and not again.steps


def test_duplicate_continuation_is_rejected_without_replanning(state):
    _, owner, task_id = state
    asyncio.run(owner.start(task_id))
    asyncio.run(owner.start(task_id, answer='Save JSON only.'))
    with pytest.raises(ExecutionError):
        asyncio.run(owner.start(task_id, answer='Duplicate answer'))
    assert owner.planner.plan.await_count == 2


def test_tool_failure_records_no_success_or_test_pass(state):
    from app.execution.security import ToolFailure
    from app.execution.store import response
    _, owner, task_id = state
    asyncio.run(owner.start(task_id))
    run = asyncio.run(owner.start(task_id, answer='Save JSON only.'))
    token = owner.approve(task_id, ApprovalRequest(plan_digest=run.plan_digest, decision='approve'))
    def failed_tool(call): raise ToolFailure('Injected failure; no filesystem tool used')
    owner.executor = SimpleNamespace(execute=failed_tool)
    owner.execute(task_id, token)
    saved = response(owner.path, task_id)
    assert saved.phase == 'failed' and saved.steps[0].result is None
    assert saved.steps[1].attempts == 0
    with connection(owner.path) as db:
        final = db.execute('SELECT final_result FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
    assert 'NOT VERIFIED' in final and 'NOT TESTED' in final and 'PASSED' not in final


def test_migration_preserves_rows_and_corrects_legacy_clarification(tmp_path):
    path = tmp_path/'migration.db'
    initialize_database(path)
    task = create_task(path, TaskCreate(description='Original requirements'))
    with connection(path) as db:
        # Re-enter the previous schema version, with representative legacy data.
        db.execute('DELETE FROM schema_version WHERE version=9')
        db.execute('DROP TABLE execution_clarifications')
        db.execute("UPDATE tasks SET status='completed' WHERE id=?", (str(task.id),))
        db.execute('INSERT INTO chat_runs VALUES (?,?,?,?)', (str(task.id),'old-owner',proposal('Which date?').display_plan().model_dump_json(),'[]'))
    initialize_database(path)
    initialize_database(path)
    with connection(path) as db:
        saved = db.execute('SELECT * FROM tasks').fetchone()
        assert saved['description'] == 'Original requirements' and saved['status'] == 'awaiting_user_input'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert db.execute('SELECT count(*) FROM task_history WHERE task_id=?', (str(task.id),)).fetchone()[0] == 2


def test_disabled_and_host_capabilities_are_explicit(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path/'safe.db')
    assert settings.execution_enabled is settings.python_execution_enabled is False
    with TestClient(create_app(settings), base_url='http://127.0.0.1', client=('127.0.0.1',1234)) as client:
        runtime = client.get('/api/v1/runtime').json()
        assert runtime['workspace_supported'] == workspace_supported()
        assert 'web app cannot' in runtime['execution_limitations']
        task_id = client.post('/api/tasks', json={'description':FEST}).json()['id']
        assert client.post(f'/api/tasks/{task_id}/orchestrate').status_code == 503
        assert client.get(f'/api/tasks/{task_id}').json()['status'] == 'blocked'


@pytest.mark.skipif(not workspace_supported(), reason='Real restricted workspace I/O: NOT TESTED on this host')
@pytest.mark.parametrize('failure', [False, True])
def test_real_tools_approval_failure_and_final_evidence(tmp_path, failure):
    root = tmp_path/'workspace'; root.mkdir()
    if failure: (root/'fest.json').write_text('existing file')
    settings = Settings(_env_file=None, database_path=tmp_path/'real.db', workspace_path=root,
                        execution_enabled=True, openai_api_key='test-placeholder-only')
    with TestClient(create_app(settings), base_url='http://127.0.0.1', client=('127.0.0.1',1234)) as client:
        owner = client.app.state.orchestrator
        owner.planner = SimpleNamespace(plan=AsyncMock(return_value=proposal()))
        task_id = client.post('/api/tasks', json={'description':'Create fest.json with budget 25000 and validate budget.'}).json()['id']
        url = f'/api/tasks/{task_id}'
        run = client.post(url+'/orchestrate').json()
        assert client.post(url+'/approval', json={'plan_digest':'0'*64,'decision':'approve'}).status_code == 409
        assert (root/'fest.json').exists() == failure
        final = client.post(url+'/approval', json={'plan_digest':run['plan_digest'],'decision':'approve'}).json()
        assert final['phase'] == ('failed' if failure else 'completed')
        task = client.get(url).json()
        assert 'NOT TESTED' in task['final_result']
        if failure:
            assert (root/'fest.json').read_text() == 'existing file'
            assert final['steps'][0]['result'] is None
        else:
            assert json.loads((root/'fest.json').read_text()) == {'budget':25000}
            assert all(s['result'] and s['verification']['passed'] for s in final['steps'])
            assert 'validate_json(fest.json' in task['final_result'] and 'PASSED' in task['final_result']
