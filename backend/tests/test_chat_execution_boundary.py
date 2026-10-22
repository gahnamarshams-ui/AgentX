"""Completion and same-task continuation must not depend on model honesty."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI as RealAsyncOpenAI

from app import ai
from app.config import Settings
from app.database import connection
from app.main import create_app
from app.plan_schema import TaskPlan
from app.responses import Answer, ResponseExecutor

REQUEST = 'Create a responsive task management website with HTML, CSS and JavaScript. Include adding tasks, deleting tasks, marking tasks complete, and saving tasks in localStorage. Generate actual project files and verify the implementation.'


def proposal(question=False):
    return TaskPlan(goal=REQUEST, assumptions=[], constraints=[], clarification_needed=question,
        clarification_questions=['Which storage?'] if question else [], steps=[dict(number=1,
        action='Implement task website', purpose='Satisfy the request', expected_output='Three project files', dependencies=[])])


@pytest.fixture
def setup(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path/'test.db', workspace_path=tmp_path/'untouched',
                        openai_api_key='test-placeholder-only', execution_enabled=False, python_execution_enabled=False)
    with TestClient(create_app(settings)) as client:
        yield settings, client


@pytest.mark.parametrize('message', ['BLOCKED; no files created.', 'All implemented and all tests passed!'])
def test_implementation_never_completes_from_model_text(setup, monkeypatch, message):
    settings, client = setup
    monkeypatch.setattr(ai, 'generate_plan', AsyncMock(return_value=proposal()))
    answer = AsyncMock(return_value=Answer(markdown=message))
    monkeypatch.setattr(ResponseExecutor, 'answer', answer)
    identifier = client.post('/api/tasks', json={'description':REQUEST}).json()['id']
    result = client.post(f'/api/tasks/{identifier}/respond').json()
    assert result['status'] == 'blocked' and 'NOT TESTED' in result['final_result']
    answer.assert_not_awaited()
    states = client.get(f'/api/tasks/{identifier}/activity').json()['agents']
    assert [s['role'] for s in states] == ['Orchestrator','Planner','Safety','Executor','Verifier']
    assert states[3]['status'] == 'skipped'
    assert states[4]['text_verification']['code_executed'] is False
    with connection(settings.database_path) as db:
        assert db.execute('SELECT execution_required FROM tasks WHERE id=?', (identifier,)).fetchone()[0] == 1
        for table in ['execution_steps', 'execution_approvals', 'python_jobs']:
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert not settings.workspace_path.exists()


def test_same_task_clarification_and_failed_retry_preserve_inputs(setup, monkeypatch):
    settings, client = setup
    planner = AsyncMock(side_effect=[proposal(True), ai.PlanningError('invalid_ai_response','Invalid model output.',502), proposal()])
    monkeypatch.setattr(ai, 'generate_plan', planner)
    identifier = client.post('/api/tasks',json={'description':REQUEST}).json()['id']
    url = f'/api/tasks/{identifier}/respond'
    assert client.post(url).json()['status'] == 'awaiting_user_input'
    assert client.post(url, json={'answer':'Use localStorage and vanilla JavaScript.'}).status_code == 502
    assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'failed'
    result = client.post(url).json()
    assert result['id'] == identifier and result['description'] == REQUEST and result['status'] == 'blocked'
    for call in planner.call_args_list[1:]:
        payload = json.loads(call.args[1])
        assert payload['original_request'] == REQUEST
        assert payload['clarification_answers'] == ['Use localStorage and vanilla JavaScript.']
    assert client.post(url,json={'answer':'Another answer'}).status_code == 409
    with connection(settings.database_path) as db:
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM chat_clarifications').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM execution_approvals').fetchone()[0] == 0


def test_followup_inherits_persisted_execution_intent(setup, monkeypatch):
    settings, client = setup
    monkeypatch.setattr(ai,'generate_plan',AsyncMock(return_value=proposal()))
    root = client.post('/api/tasks',json={'description':REQUEST}).json()['id']
    assert client.post(f'/api/tasks/{root}/respond').json()['status'] == 'blocked'
    child = client.post('/api/tasks',json={'description':'Proceed with those defaults.', 'conversation_id':root}).json()['id']
    assert client.post(f'/api/tasks/{child}/respond').json()['status'] == 'blocked'
    with connection(settings.database_path) as db:
        assert db.execute('SELECT execution_required FROM tasks WHERE id=?',(child,)).fetchone()[0] == 1


def test_disabled_orchestration_is_blocked_without_approval_or_tools(tmp_path):
    settings = Settings(_env_file=None,database_path=tmp_path/'test.db',workspace_path=tmp_path/'untouched')
    with TestClient(create_app(settings),base_url='http://127.0.0.1',client=('127.0.0.1',1234)) as client:
        identifier = client.post('/api/tasks',json={'description':REQUEST}).json()['id']
        url = f'/api/tasks/{identifier}'
        assert client.post(url+'/orchestrate').status_code == 503
        assert client.get(url).json()['status'] == 'blocked'
        assert client.post(url+'/approval',json={'decision':'approve','plan_digest':'0'*64}).status_code == 503
        with connection(settings.database_path) as db:
            for table in ['execution_runs','execution_steps','execution_approvals']:
                assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert not settings.workspace_path.exists()


@pytest.mark.parametrize('status,text,reason,expected', [
    ('incomplete', '{"markdown":"truncated', 'max_output_tokens', 'ai_incomplete_response'),
    ('completed', '{"markdown":"truncated', None, 'invalid_ai_response'),
    ('completed', '{"markdown": 123}', None, 'invalid_ai_response'),
    ('completed', '{"markdown":"A valid answer."}', None, None),
])
def test_actual_sdk_checks_status_before_parsing(setup, monkeypatch, status, text, reason, expected):
    import asyncio
    settings, _ = setup
    def transport(request):
        body = json.loads(request.content)
        assert body['text']['format']['strict'] and body['store'] is False and 'tools' not in body
        return httpx2.Response(200,json=dict(id='resp_test',object='response',created_at=1,model='gpt-4.1-mini',
            status=status,incomplete_details={'reason':reason} if reason else None,
            output=[dict(id='msg_test',type='message',role='assistant',status=status,
                         content=[dict(type='output_text',text=text,annotations=[])])]))
    monkeypatch.setattr(ai,'AsyncOpenAI',lambda **kw: RealAsyncOpenAI(**kw,http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport))))
    if expected:
        with pytest.raises(ai.PlanningError) as exc:
            asyncio.run(ai.generate_structured(settings,'A bounded question',Answer,'Answer briefly.'))
        assert exc.value.code == expected
        assert 'truncated' not in exc.value.message
    else:
        assert asyncio.run(ai.generate_structured(settings,'A bounded question',Answer,'Answer briefly.')).markdown == 'A valid answer.'
