"""Persistent conversation metadata and read-only activity; no live tools or APIs."""
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import connection
from app.main import create_app
from app.plan_schema import TaskPlan


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_path=tmp_path / 'tasks.sqlite3', workspace_path=tmp_path / 'unused', execution_enabled=False, openai_api_key='test-only')


def local(settings):
    return TestClient(create_app(settings), base_url='http://127.0.0.1', client=('127.0.0.1', 1234))


def create(client, description='Original goal'):
    return client.post('/api/tasks', json={'description': description}).json()['id']


def test_rename_archive_restore_search_and_restart(settings):
    with local(settings) as c:
        identifier = create(c)
        url = '/api/tasks/' + identifier
        assert c.post(url + '/conversation', json={'title': ' New title ', 'archived': True}).json()['title'] == 'New title'
        assert c.get('/api/tasks?archived=false').json()['tasks'] == []
        assert c.get('/api/tasks?archived=true&q=new%20title').json()['tasks'][0]['id'] == identifier
        assert c.get('/api/tasks?q=original').json()['tasks'][0]['description'] == 'Original goal'
        assert c.get('/api/tasks?q=saved').json()['tasks'][0]['id'] == identifier  # history text
        assert c.get('/api/tasks?q=%25').json()['tasks'] == []  # literal wildcard
        assert c.get('/api/tasks?q=%27%20OR%201=1--').json()['tasks'] == []
    with local(settings) as c:
        assert c.get(url).json()['archived'] is True
        assert c.post(url + '/conversation', json={'archived': False}).json()['archived'] is False
        assert c.get('/api/tasks?archived=false&q=new').json()['tasks'][0]['id'] == identifier
        assert len(c.get(url + '/history').json()['events']) == 1  # metadata doesn't fake status transitions


@pytest.mark.parametrize('body', [{}, {'title':' '}, {'title':'x'*121}, {'archived':'true'}, {'status':'completed'}])
def test_invalid_metadata_is_rejected(settings, body):
    with local(settings) as c:
        url = '/api/tasks/' + create(c)
        assert c.post(url + '/conversation', json=body).status_code == 422
        assert c.get(url).json()['title'] is None


@pytest.mark.parametrize('headers', [{'origin':'https://evil.example'}, {'host':'evil.example'}, {'sec-fetch-site':'cross-site'}])
def test_mutations_are_local_and_same_origin(settings, headers):
    with local(settings) as c:
        url = '/api/tasks/' + create(c)
        assert c.post(url + '/conversation', json={'title':'changed'}, headers=headers).status_code == 403
        assert c.delete(url, headers=headers).status_code == 403
        assert c.get(url).status_code == 200


def test_delete_is_atomic_removes_history_and_blocks_active(settings):
    with local(settings) as c:
        identifier = create(c); url = '/api/tasks/' + identifier
        with connection(settings.database_path) as db:
            db.execute("UPDATE tasks SET status='planning' WHERE id=?", (identifier,))
        assert c.delete(url).status_code == 409
        with connection(settings.database_path) as db:
            db.execute("UPDATE tasks SET status='pending' WHERE id=?", (identifier,))
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON tasks BEGIN SELECT RAISE(ABORT,'private detail'); END")
        assert c.delete(url).status_code == 503
        assert len(c.get(url + '/history').json()['events']) == 1
        with connection(settings.database_path) as db:
            db.execute('DROP TRIGGER fail_delete')
        assert c.delete(url).json() == {'deleted': True, 'id': identifier}
        for suffix in ['', '/history', '/activity']:
            assert c.get(url + suffix).status_code == 404
        assert c.get('/api/tasks').json()['tasks'] == []
        assert c.delete(url).status_code == 404
        assert c.post('/api/tasks/' + str(uuid4()) + '/conversation', json={'title':'x'}).status_code == 404
        assert not settings.workspace_path.exists()


def test_proposal_activity_persists_exact_roles_without_execution(settings, monkeypatch):
    from app import planning
    from app.execution.agents import WorkspaceExecutor
    async def proposal(*args):
        return TaskPlan(goal='Learn variables', assumptions=[], constraints=['No tools'], clarification_needed=False,
            clarification_questions=[], steps=[dict(number=1, action='Study variables', purpose='Learn', expected_output='Understanding', dependencies=[])])
    def forbidden(*args):
        pytest.fail('Planning must execute zero tools')
    monkeypatch.setattr(planning, 'generate_plan', proposal)
    monkeypatch.setattr(WorkspaceExecutor, 'execute', forbidden)
    with local(settings) as c:
        identifier = create(c); url = '/api/tasks/' + identifier
        assert c.get(url + '/activity').json()['agents'][1]['status'] == 'pending'
        assert c.post(url + '/plan').json()['status'] == 'awaiting_approval'
        activity = c.get(url + '/activity').json()
        assert activity['plan_digest'] is None
        assert [a['role'] for a in activity['agents']] == ['Orchestrator','Planner','Safety','Executor','Verifier']
        assert [a['status'] for a in activity['agents']] == ['skipped','succeeded','pending','skipped','skipped']
        with connection(settings.database_path) as db:
            assert db.execute('SELECT count(*) FROM execution_steps').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM execution_runs').fetchone()[0] == 0
        assert not settings.workspace_path.exists()
    with local(settings) as c:
        assert c.get(url + '/activity').json() == activity


@pytest.mark.parametrize('origin', ['http://127.0.0.1:5173', 'http://localhost:5173'])
def test_updated_conversation_routes_and_delete_preflight(settings, origin):
    with local(settings) as c:
        spec = c.get('/openapi.json').json()
        assert 'delete' in spec['paths']['/api/tasks/{task_id}']
        assert 'post' in spec['paths']['/api/tasks/{task_id}/conversation']
        assert 'get' in spec['paths']['/api/tasks/{task_id}/activity']
        result = c.options('/api/tasks/' + str(uuid4()), headers={'Origin': origin, 'Access-Control-Request-Method':'DELETE'})
        assert result.status_code == 200
        assert result.headers['access-control-allow-origin'] == origin
        assert 'DELETE' in result.headers['access-control-allow-methods']
        assert c.options('/api/tasks/' + str(uuid4()), headers={'Origin':'https://evil.example','Access-Control-Request-Method':'DELETE'}).status_code == 400
