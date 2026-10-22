"""In-process verification when pytest is unavailable; run from backend/."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.database import connection, initialize_database
from app.main import create_app
from app.schemas import APIModel


class FoundationSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(_env_file=None, openai_api_key=None,
                                 database_path=Path(self.temp.name) / 'db.sqlite3',
                                 cors_origins=['http://localhost:5173'])

    def test_health_without_key(self):
        with TestClient(create_app(self.settings)) as client:
            response = client.get('/api/v1/health')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'status': 'ok', 'service': 'agentx-api', 'database': 'ok', 'phase': 1})

    def test_database_idempotency_and_foreign_keys(self):
        for _ in range(2):
            initialize_database(self.settings.database_path)
        with connection(self.settings.database_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_version WHERE version = 1').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_cors_allowlist(self):
        with TestClient(create_app(self.settings)) as client:
            for origin, code in [('http://localhost:5173', 200), ('https://untrusted.example', 400)]:
                response = client.options('/api/v1/health', headers={'Origin': origin, 'Access-Control-Request-Method': 'GET'})
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.headers.get('access-control-allow-origin'), origin if code == 200 else None)

    def test_configuration_validation(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, cors_origins=['*'])
        with patch.dict(os.environ, {'CORS_ORIGINS': 'not-json'}):
            with self.assertRaisesRegex(RuntimeError, 'Invalid backend environment configuration'):
                create_app()

    def test_error_contracts(self):
        class Probe(APIModel):
            count: int
        app = create_app(self.settings)
        @app.post('/test-only')
        def probe(body: Probe):
            return body
        with TestClient(app) as client:
            self.assertEqual(client.get('/missing').json()['error']['code'], 'http_error')
            response = client.post('/test-only', json={'count': 'invalid'})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['error']['code'], 'validation_error')
            with connection(self.settings.database_path) as db:
                db.execute('DROP TABLE schema_version')
            response = client.get('/api/v1/health')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['error']['code'], 'database_unavailable')

    def test_env_loading_and_secret_redaction(self):
        env = Path(self.temp.name) / '.env'
        env.write_text('OPENAI_API_KEY=test-placeholder-only\nDATABASE_PATH=data/check.sqlite3\n')
        settings = Settings(_env_file=env)
        self.assertNotIn('test-placeholder-only', repr(settings))
        self.assertTrue(settings.database_path.is_absolute())


    def test_task_creation_validation_and_lookup(self):
        with TestClient(create_app(self.settings)) as client:
            for description in ['', '   ', 'x' * 5001, None, 123]:
                self.assertEqual(client.post('/api/tasks', json={'description': description}).status_code, 422)
            response = client.post('/api/tasks', json={'description': '  Persist this task  '})
            self.assertEqual(response.status_code, 201)
            task = response.json()
            self.assertEqual(task['description'], 'Persist this task')
            self.assertEqual(task['status'], 'pending')
            self.assertIsNone(task['plan'])
            self.assertIsNone(task['final_result'])
            self.assertEqual(client.get(f"/api/tasks/{task['id']}").json(), task)
            self.assertEqual(client.get('/api/tasks/00000000-0000-4000-8000-000000000000').status_code, 404)
            self.assertEqual(client.get('/api/tasks?limit=101').status_code, 422)
            self.assertEqual(client.get('/api/tasks').json()['tasks'], [task])

    def test_task_history_and_restart(self):
        from app.tasks import cancel_pending_task
        with TestClient(create_app(self.settings)) as client:
            first = client.post('/api/tasks', json={'description': 'First'}).json()
            second = client.post('/api/tasks', json={'description': 'Second'}).json()
            self.assertEqual([t['id'] for t in client.get('/api/tasks').json()['tasks']], [second['id'], first['id']])
        cancel_pending_task(self.settings.database_path, first['id'])
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get(f"/api/tasks/{first['id']}").json()['status'], 'cancelled')
            events = client.get(f"/api/tasks/{first['id']}/history").json()['events']
            self.assertEqual([e['event_type'] for e in events], ['task_created', 'status_changed'])
            self.assertEqual([e['created_at'] for e in events], sorted(e['created_at'] for e in events))

    def test_task_transaction_rollback_and_safe_database_failure(self):
        with TestClient(create_app(self.settings)) as client:
            with connection(self.settings.database_path) as db:
                db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON task_history BEGIN SELECT RAISE(ABORT, 'private storage error'); END")
            response = client.post('/api/tasks', json={'description': 'Must roll back'})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private storage error', response.text)
            self.assertEqual(client.get('/api/tasks').json()['tasks'], [])

    def test_v1_upgrade_preserves_existing_data(self):
        with connection(self.settings.database_path) as db:
            db.execute('CREATE TABLE schema_version (version INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO schema_version VALUES (1)')
            db.execute('CREATE TABLE legacy_marker (value TEXT)')
            db.execute("INSERT INTO legacy_marker VALUES ('preserved')")
        initialize_database(self.settings.database_path)
        initialize_database(self.settings.database_path)
        with connection(self.settings.database_path) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT version FROM schema_version ORDER BY version')], [1, 2, 3, 4, 5, 6, 7, 8])
            self.assertEqual(db.execute('SELECT value FROM legacy_marker').fetchone()[0], 'preserved')


if __name__ == '__main__':
    unittest.main(verbosity=2)
