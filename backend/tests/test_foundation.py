import sqlite3
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.database import connection, initialize_database
from app.main import create_app
from app.schemas import APIModel


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "DATABASE_PATH", "CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, database_path=tmp_path / "db" / "test.sqlite3")


def test_health_without_openai_key(settings):
    assert settings.openai_api_key is None
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "service": "agentx-api", "database": "ok", "phase": 1}
    assert settings.database_path.is_file()


def test_database_initialization_is_repeatable(settings):
    initialize_database(settings.database_path)
    initialize_database(settings.database_path)
    with connection(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM schema_version WHERE version = 1").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_cors(settings):
    with TestClient(create_app(settings)) as client:
        for origin, expected in [("http://localhost:5173", 200), ("https://untrusted.example", 400)]:
            result = client.options("/api/v1/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
            assert result.status_code == expected
            assert (result.headers.get("access-control-allow-origin") == origin) is (expected == 200)


@pytest.mark.parametrize("origin", ["*", "https://*.example.com", "https://example.com/path", "file://example.com"])
def test_invalid_cors_rejected(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_origins=[origin])


def test_environment_and_secret_redaction(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "from-env.sqlite3"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-not-a-real-key")
    config = Settings(_env_file=None)
    assert config.database_path == tmp_path / "from-env.sqlite3"
    assert "test-placeholder-not-a-real-key" not in repr(config)


def test_missing_route_error_contract(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/missing")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "http_error"


def test_database_failure_returns_503(settings):
    with TestClient(create_app(settings)) as client:
        with connection(settings.database_path) as db:
            db.execute("DROP TABLE schema_version")
        response = client.get("/api/v1/health")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_unavailable"


def test_request_validation_contract(settings):
    class ProbeRequest(APIModel):
        count: int
    app = create_app(settings)
    @app.post("/test-only-validation")
    def probe(body: ProbeRequest):
        return body
    with TestClient(app) as client:
        response = client.post("/test-only-validation", json={"count": "invalid", "extra": True})
        assert response.status_code == 422
        assert response.json() == {"error": {"code": "validation_error", "message": "Request failed validation."}}


def test_startup_database_error_is_clear(settings, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    settings.database_path = blocker / "db.sqlite3"
    with pytest.raises(RuntimeError, match="SQLite initialization failed"):
        with TestClient(create_app(settings)):
            pass


def test_runtime_reports_process_revision_and_actual_disabled_flags_without_secrets(settings):
    import os
    from pydantic import SecretStr
    settings.openai_api_key = SecretStr('test-runtime-secret-never-return')
    with TestClient(create_app(settings)) as client:
        response = client.get('/api/v1/runtime')
        assert response.status_code == 200
        runtime = response.json()
        assert runtime['process_id'] == os.getpid()
        assert runtime['chat_pipeline_revision']
        assert runtime['execution_enabled'] is runtime['python_execution_enabled'] is False
        assert runtime['restart_required'] is False
        assert runtime['source_snapshot_sha256'] == runtime['current_source_sha256']
        assert len(runtime['source_snapshot_sha256']) == 64
        assert settings.openai_api_key.get_secret_value() not in response.text
        assert 'database_path' not in runtime and 'environment' not in runtime


def test_runtime_detects_source_changed_since_process_import(settings, monkeypatch):
    from app import main
    with TestClient(create_app(settings)) as client:
        before = client.get('/api/v1/runtime').json()
        monkeypatch.setattr(main, 'chat_source_fingerprint', lambda: '0'*64)
        after = client.get('/api/v1/runtime').json()
        assert after['restart_required'] is True
        assert after['source_snapshot_sha256'] == before['source_snapshot_sha256']
        assert after['current_source_sha256'] == '0'*64
