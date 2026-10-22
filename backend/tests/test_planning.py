import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from conftest import ProviderResponse as SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import APITimeoutError, APIConnectionError, RateLimitError, AuthenticationError, InternalServerError
from pydantic import ValidationError
from app import ai
from app.config import Settings
from app.database import connection
from app.main import create_app
from app.plan_schema import TaskPlan
from app.planning import claim, finish, recover_expired

PLAN = {'goal': 'Prepare a study schedule', 'assumptions': ['Daily study is possible'], 'constraints': [],
        'clarification_needed': True, 'clarification_questions': ['What is the exam date?'], 'steps': [
    {'number': 1, 'action': 'Confirm exam date', 'purpose': 'Bound the schedule', 'expected_output': 'Confirmed date', 'dependencies': []},
    {'number': 2, 'action': 'Draft schedule', 'purpose': 'Allocate time', 'expected_output': 'Proposed schedule', 'dependencies': [1]}]}

@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_path=tmp_path / 'planning.sqlite3', openai_api_key='test-placeholder-only')

@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=SimpleNamespace(status='completed', output_parsed=TaskPlan.model_validate(PLAN)))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=client)
    manager.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=manager)
    monkeypatch.setattr(ai, 'AsyncOpenAI', factory)
    return client, factory

def create(client):
    return client.post('/api/tasks', json={'description': 'Plan my exam preparation'}).json()['id']

def test_valid_plan():
    assert TaskPlan.model_validate(PLAN).steps[1].dependencies == [1]

@pytest.mark.parametrize('change', ['empty', 'too_many', 'long', 'forward', 'duplicate', 'number', 'clarification', 'extra', 'coercion'])
def test_reject_invalid_plans(change):
    value = copy.deepcopy(PLAN)
    if change == 'empty': value['steps'] = []
    if change == 'too_many': value['steps'] *= 7
    if change == 'long': value['goal'] = 'x' * 1001
    if change == 'forward': value['steps'][0]['dependencies'] = [2]
    if change == 'duplicate': value['steps'][1]['dependencies'] = [1, 1]
    if change == 'number': value['steps'][1]['number'] = 3
    if change == 'clarification': value['clarification_needed'] = False
    if change == 'extra': value['command'] = 'untrusted'
    if change == 'coercion': value['steps'][0]['number'] = '1'
    with pytest.raises(ValidationError): TaskPlan.model_validate(value)

def test_success_and_saved_history(settings, sdk):
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        response = client.post(f'/api/tasks/{identifier}/plan')
        assert response.status_code == 200
        task = response.json()
        assert task['status'] == 'awaiting_user_input' and task['plan'] == PLAN and task['final_result'] is None
        assert [e['status'] for e in client.get(f'/api/tasks/{identifier}/history').json()['events']] == ['pending', 'planning', 'awaiting_user_input']
        assert client.post(f'/api/tasks/{identifier}/plan').status_code == 409
        assert sdk[0].responses.create.await_count == 1
    with TestClient(create_app(settings)) as client:
        assert client.get(f'/api/tasks/{identifier}').json()['plan'] == PLAN
    kwargs = sdk[0].responses.create.call_args.kwargs
    assert kwargs['text']['format']['name'] == 'TaskPlan' and kwargs['store'] is False and 'tools' not in kwargs
    assert sdk[1].call_args.kwargs['max_retries'] == 0

def test_missing_key_and_missing_task(settings, sdk):
    settings.openai_api_key = None
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        response = client.post(f'/api/tasks/{identifier}/plan')
        assert response.status_code == 503 and response.json()['error']['code'] == 'ai_not_configured'
        assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'pending'
        assert client.post(f'/api/tasks/{uuid4()}/plan').status_code == 404
        assert client.post('/api/tasks/not-uuid/plan').status_code == 422
        sdk[1].assert_not_called()

def provider_error(kind):
    request = httpx2.Request('POST', 'https://api.openai.com/v1/responses')
    if kind == 'timeout': return APITimeoutError(request=request)
    if kind == 'connection': return APIConnectionError(request=request)
    cls, status = {'rate': (RateLimitError, 429), 'auth': (AuthenticationError, 401), 'server': (InternalServerError, 500)}[kind]
    return cls('private-provider-detail', response=httpx2.Response(status, request=request), body={'secret': 'private-provider-detail'})

@pytest.mark.parametrize('kind,expected', [('timeout', 504), ('connection', 503), ('rate', 429), ('auth', 503), ('server', 502)])
def test_provider_failures_are_safe_and_retryable(settings, sdk, kind, expected):
    sdk[0].responses.create.side_effect = provider_error(kind)
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        response = client.post(f'/api/tasks/{identifier}/plan')
        assert response.status_code == expected and 'private-provider-detail' not in response.text
        task = client.get(f'/api/tasks/{identifier}').json()
        assert task['status'] == 'failed' and task['plan'] is None and 'private-provider-detail' not in task['error']
        assert client.get(f'/api/tasks/{identifier}/history').json()['events'][-1]['status'] == 'failed'
        sdk[0].responses.create.side_effect = None
        assert client.post(f'/api/tasks/{identifier}/plan').status_code == 200

@pytest.mark.parametrize('result', [None, SimpleNamespace(model_dump=lambda: {'invalid': 'data'})])
def test_malformed_output_never_saved(settings, sdk, result):
    sdk[0].responses.create.return_value = SimpleNamespace(status='completed', output_parsed=result)
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        assert client.post(f'/api/tasks/{identifier}/plan').status_code == 502
        task = client.get(f'/api/tasks/{identifier}').json()
        assert task['status'] == 'failed' and task['plan'] is None

def test_incomplete_response(settings, sdk):
    sdk[0].responses.create.return_value.status = 'incomplete'
    with TestClient(create_app(settings)) as client:
        assert client.post(f'/api/tasks/{create(client)}/plan').status_code == 502

def test_concurrency_and_unlocked_database(settings, sdk):
    entered, release = threading.Event(), threading.Event()
    async def delayed(**kwargs):
        entered.set()
        await asyncio.to_thread(release.wait, 3)
        return SimpleNamespace(status='completed', output_parsed=TaskPlan.model_validate(PLAN))
    sdk[0].responses.create.side_effect = delayed
    with TestClient(create_app(settings)) as client, ThreadPoolExecutor() as pool:
        identifier = create(client)
        first = pool.submit(client.post, f'/api/tasks/{identifier}/plan')
        assert entered.wait(2)
        try:
            assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'planning'
            assert client.post(f'/api/tasks/{identifier}/plan').status_code == 409
            assert client.post('/api/tasks', json={'description': 'Independent write'}).status_code == 201
        finally: release.set()
        assert first.result(timeout=5).status_code == 200
        assert sdk[0].responses.create.await_count == 1

def test_recovery_and_stale_completion(settings, sdk):
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        old, _ = claim(settings, identifier)
        with connection(settings.database_path) as db:
            db.execute("UPDATE planning_attempts SET expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (identifier,))
        recover_expired(settings.database_path); recover_expired(settings.database_path)
        assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'failed'
        new, _ = claim(settings, identifier)
        with pytest.raises(ai.PlanningError): finish(settings.database_path, identifier, old, plan=TaskPlan.model_validate(PLAN))
        finish(settings.database_path, identifier, new, plan=TaskPlan.model_validate(PLAN))
        assert [e['status'] for e in client.get(f'/api/tasks/{identifier}/history').json()['events']] == ['pending', 'planning', 'failed', 'planning', 'awaiting_user_input']

def test_restart_recovers_expired_not_live(settings, sdk):
    with TestClient(create_app(settings)) as client:
        identifier, live = create(client), create(client)
        claim(settings, identifier); claim(settings, live)
        with connection(settings.database_path) as db:
            db.execute("UPDATE planning_attempts SET expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (identifier,))
    with TestClient(create_app(settings)) as client:
        assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'failed'
        assert client.get(f'/api/tasks/{live}').json()['status'] == 'planning'

def test_unrelated_failure_ineligible(settings, sdk):
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        with connection(settings.database_path) as db: db.execute("UPDATE tasks SET status='failed' WHERE id=?", (identifier,))
        assert client.post(f'/api/tasks/{identifier}/plan').status_code == 409
        sdk[1].assert_not_called()

def test_completion_rollback(settings, sdk):
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        with connection(settings.database_path) as db:
            db.execute("CREATE TRIGGER reject_plan BEFORE INSERT ON task_history WHEN NEW.status='awaiting_user_input' BEGIN SELECT RAISE(ABORT,'private db detail'); END")
        assert client.post(f'/api/tasks/{identifier}/plan').status_code == 503
        task = client.get(f'/api/tasks/{identifier}').json()
        assert task['status'] == 'failed' and task['plan'] is None and 'private db detail' not in task['error']

def test_claim_rollback(settings, sdk):
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        with connection(settings.database_path) as db:
            db.execute("CREATE TRIGGER reject_start BEFORE INSERT ON task_history WHEN NEW.status='planning' BEGIN SELECT RAISE(ABORT,'failure'); END")
        assert client.post(f'/api/tasks/{identifier}/plan').status_code == 503
        assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'pending'
        sdk[1].assert_not_called()

def test_total_deadline(settings, sdk):
    async def stalled(**kwargs):
        await asyncio.sleep(1)
    sdk[0].responses.create.side_effect = stalled
    # Shorten only this test's deadline without relaxing production config bounds.
    test_settings = settings.model_copy(update={'openai_timeout_seconds': 0.01})
    with TestClient(create_app(test_settings)) as client:
        identifier = create(client)
        assert client.post(f'/api/tasks/{identifier}/plan').status_code == 504
        assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'failed'


def test_actual_sdk_structured_parser_with_mock_transport(settings, monkeypatch):
    import json
    from openai import AsyncOpenAI as RealAsyncOpenAI
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={
            'id': 'resp_test', 'object': 'response', 'created_at': 1, 'status': 'completed',
            'model': 'gpt-4.1-mini', 'error': None, 'incomplete_details': None,
            'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': json.dumps(PLAN), 'annotations': []}]}],
        })
    def factory(**kwargs):
        return RealAsyncOpenAI(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
    monkeypatch.setattr(ai, 'AsyncOpenAI', factory)
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        response = client.post(f'/api/tasks/{identifier}/plan')
        assert response.status_code == 200
        assert response.json()['plan'] == PLAN
    assert requests[0]['text']['format']['type'] == 'json_schema'
    assert requests[0]['text']['format']['strict'] is True


def test_recovery_after_failure_cannot_be_written(settings, sdk):
    sdk[0].responses.create.side_effect = provider_error('timeout')
    with TestClient(create_app(settings)) as client:
        identifier = create(client)
        with connection(settings.database_path) as db:
            db.execute("CREATE TRIGGER reject_failure BEFORE INSERT ON task_history WHEN NEW.status='failed' BEGIN SELECT RAISE(ABORT,'unavailable'); END")
        assert client.post(f'/api/tasks/{identifier}/plan').status_code == 504
        # All-or-nothing history write leaves planning; expiry recovery retries later.
        assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'planning'
        with connection(settings.database_path) as db:
            db.execute('DROP TRIGGER reject_failure')
            db.execute("UPDATE planning_attempts SET expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (identifier,))
        recover_expired(settings.database_path)
        assert client.get(f'/api/tasks/{identifier}').json()['status'] == 'failed'
