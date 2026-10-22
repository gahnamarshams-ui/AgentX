import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import connection, initialize_database
from app.main import create_app
from app.tasks import cancel_pending_task


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, openai_api_key=None, database_path=tmp_path / 'tasks.sqlite3')


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def test_create_is_pending_and_records_history(client):
    response = client.post('/api/tasks', json={'description': '  Prepare a study plan  '})
    assert response.status_code == 201
    task = response.json()
    assert task['description'] == 'Prepare a study plan'
    assert task['status'] == 'pending'
    assert task['plan'] is None and task['final_result'] is None and task['error'] is None
    assert task['created_at'] == task['updated_at']
    events = client.get(f"/api/tasks/{task['id']}/history").json()['events']
    assert len(events) == 1
    assert events[0]['event_type'] == 'task_created'
    assert events[0]['status'] == 'pending'


@pytest.mark.parametrize('body', [
    {}, {'description': ''}, {'description': ' \n\t'}, {'description': 'x' * 5001},
    {'description': 123}, {'description': None}, {'description': 'ok', 'status': 'completed'},
])
def test_invalid_descriptions_leave_no_rows(client, body):
    response = client.post('/api/tasks', json=body)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'validation_error'
    assert client.get('/api/tasks').json()['tasks'] == []


def test_description_boundary_and_sql_literals(client):
    assert client.post('/api/tasks', json={'description': 'x' * 5000}).status_code == 201
    text = "Robert'); DROP TABLE tasks; --"
    response = client.post('/api/tasks', json={'description': text})
    assert response.status_code == 201
    assert client.get(f"/api/tasks/{response.json()['id']}").json()['description'] == text


def test_list_newest_first_and_pagination(client):
    first = client.post('/api/tasks', json={'description': 'First'}).json()
    second = client.post('/api/tasks', json={'description': 'Second'}).json()
    assert [task['id'] for task in client.get('/api/tasks').json()['tasks']] == [second['id'], first['id']]
    assert client.get('/api/tasks?limit=1&offset=1').json() == {'tasks': [first], 'limit': 1, 'offset': 1}


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'limit=abc', 'offset=-1', 'offset=100001'])
def test_invalid_pagination(client, query):
    assert client.get(f'/api/tasks?{query}').status_code == 422


def test_lookup_missing_and_malformed_ids(client):
    task = client.post('/api/tasks', json={'description': 'Look me up'}).json()
    assert client.get(f"/api/tasks/{task['id']}").json() == task
    for suffix in ['', '/history']:
        response = client.get(f'/api/tasks/{uuid4()}{suffix}')
        assert response.status_code == 404
        assert response.json()['error']['message'] == 'Task not found.'
        assert client.get(f'/api/tasks/not-a-uuid{suffix}').status_code == 422


def test_history_order_transition_and_restart(settings):
    with TestClient(create_app(settings)) as client:
        task = client.post('/api/tasks', json={'description': 'Persist me'}).json()
    cancel_pending_task(settings.database_path, task['id'])
    with pytest.raises(ValueError):
        cancel_pending_task(settings.database_path, task['id'])
    with TestClient(create_app(settings)) as client:
        result = client.get(f"/api/tasks/{task['id']}").json()
        assert result['status'] == 'cancelled'
        assert result['updated_at'] >= task['updated_at']
        events = client.get(f"/api/tasks/{task['id']}/history").json()['events']
        assert [e['event_type'] for e in events] == ['task_created', 'status_changed']
        assert [e['created_at'] for e in events] == sorted(e['created_at'] for e in events)
        assert result['plan'] is None and result['final_result'] is None


def test_upgrade_v1_and_repeat_initialization(settings):
    with connection(settings.database_path) as db:
        db.execute('CREATE TABLE schema_version (version INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO schema_version VALUES (1)')
        db.execute('CREATE TABLE legacy_marker (value TEXT)')
        db.execute("INSERT INTO legacy_marker VALUES ('keep me')")
    initialize_database(settings.database_path)
    initialize_database(settings.database_path)
    with connection(settings.database_path) as db:
        assert [r[0] for r in db.execute('SELECT version FROM schema_version ORDER BY version')] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert db.execute('SELECT value FROM legacy_marker').fetchone()[0] == 'keep me'


def test_history_write_failure_rolls_back_creation(client, settings):
    with connection(settings.database_path) as db:
        db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON task_history BEGIN SELECT RAISE(ABORT, 'private database detail'); END")
    response = client.post('/api/tasks', json={'description': 'Must roll back'})
    assert response.status_code == 503
    assert 'private database detail' not in response.text
    assert client.get('/api/tasks').json()['tasks'] == []


def test_status_write_and_history_roll_back_together(client, settings):
    task = client.post('/api/tasks', json={'description': 'Keep pending'}).json()
    with connection(settings.database_path) as db:
        db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON task_history BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(sqlite3.Error):
        cancel_pending_task(settings.database_path, task['id'])
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'pending'
    assert len(client.get(f"/api/tasks/{task['id']}/history").json()['events']) == 1


def test_database_failures_are_safe(client, settings):
    with connection(settings.database_path) as db:
        db.execute('DROP TABLE task_history')
        db.execute('DROP TABLE tasks')
    for path in ['/api/tasks', f'/api/tasks/{uuid4()}', f'/api/tasks/{uuid4()}/history']:
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'database_unavailable'
        assert 'no such table' not in response.text
    assert client.post('/api/tasks', json={'description': 'Unavailable'}).status_code == 503


def test_post_cors_and_no_execution_endpoint(client):
    response = client.options('/api/tasks', headers={
        'Origin': 'http://localhost:5173', 'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type',
    })
    assert response.status_code == 200
    task = client.post('/api/tasks', json={'description': 'Do not execute'}).json()
    assert client.post(f"/api/tasks/{task['id']}/run").status_code == 404
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'pending'


def test_history_ties_are_ordered_by_event_id(client, settings):
    task = client.post('/api/tasks', json={'description': 'Ordered events'}).json()
    cancel_pending_task(settings.database_path, task['id'])
    with connection(settings.database_path) as db:
        db.execute('UPDATE task_history SET created_at = ? WHERE task_id = ?', (task['created_at'], task['id']))
    events = client.get(f"/api/tasks/{task['id']}/history").json()['events']
    assert [e['id'] for e in events] == sorted(e['id'] for e in events)
    assert [e['status'] for e in events] == ['pending', 'cancelled']
