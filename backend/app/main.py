import asyncio
import hashlib
import os
from pathlib import Path
from contextlib import suppress
import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic_settings import SettingsError
from starlette.exceptions import HTTPException

from app.config import Settings
from app.database import check_database, initialize_database
from app.schemas import ErrorResponse, HealthResponse
from app.tasks import task_router
from app.ai import PlanningError
from app.planning import planning_router, recover_expired, recover_loop

from app.execution.security import ExecutionError
from app.execution.orchestrator import Orchestrator
from app.execution.models import AGENT_ROLES
from app.execution.routes import execution_router
from app.execution.store import recover

logger = logging.getLogger(__name__)


def chat_source_fingerprint():
    """Hash application source only; never read .env, user data or workspace files."""
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ('main.py','responses.py','chat_conflicts.py','ai.py','planning.py','tasks.py','plan_schema.py','config.py','task_intent.py','database.py','schemas.py'):
        digest.update(name.encode()+b'\0'+(root/name).read_bytes()+b'\0')
    return digest.hexdigest()


# Capture at module import, not on each request: a running process must not
# advertise new disk contents as if it had reloaded them.
CHAT_SOURCE_SNAPSHOT = chat_source_fingerprint()


def error_response(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error={"code": code, "message": message})
    return JSONResponse(status_code=status, content=body.model_dump())


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        try:
            settings = Settings()
        except ValidationError as exc:
            fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
            raise RuntimeError(f"Invalid backend configuration: {fields}. Check backend/.env.") from None

        except SettingsError:
            raise RuntimeError("Invalid backend environment configuration. Check backend/.env syntax and CORS_ORIGINS JSON.") from None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            initialize_database(settings.database_path)
            recover_expired(settings.database_path)
            recover(settings.database_path)
            from app.execution.python_workflow import recover_python_jobs
            recover_python_jobs(settings)
        except (OSError, sqlite3.Error):
            raise RuntimeError("SQLite initialization failed. Check DATABASE_PATH and directory permissions.") from None
        if settings.execution_enabled:
            from app.execution.workspace import workspace_supported
            if not workspace_supported():
                raise RuntimeError('Safe handle-relative workspace access is unavailable on this host. Keep EXECUTION_ENABLED=false.')
            try:
                app.state.orchestrator = Orchestrator(settings)
            except (OSError, ValueError):
                raise RuntimeError('Workspace initialization failed. WORKSPACE_PATH must be an existing ordinary directory; AgentX never creates it automatically. Check its permissions.') from None
        recovery = asyncio.create_task(recover_loop(settings.database_path))
        try:
            yield
        finally:
            recovery.cancel()
            with suppress(asyncio.CancelledError):
                await recovery

    app = FastAPI(title="AgentX API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins,
        allow_credentials=False, allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"],
    )

    @app.exception_handler(ExecutionError)
    @app.exception_handler(PlanningError)
    async def planning_error(request: Request, exc: PlanningError):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path == "/api/tasks" and request.method == "POST":
            return error_response(422, "validation_error", "Provide a non-blank description of at most 5,000 characters and optionally a valid conversation_id.")
        if request.url.path.endswith('/approval'):
            return error_response(422, 'validation_error', 'Provide a valid task UUID, the current 64-character plan_digest, and decision approve or reject.')
        if request.url.path.startswith("/api/tasks"):
            return error_response(422, "validation_error", "Use a valid task UUID, limit from 1 to 100, and offset from 0 to 100,000.")
        return error_response(422, "validation_error", "Request failed validation.")

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error):
        logger.error("Task database operation failed: %s", type(exc).__name__)
        return error_response(503, "database_unavailable", "Task storage is unavailable. Please try again later.")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("Unhandled API failure: %s", type(exc).__name__)
        return error_response(500, "internal_error", "An unexpected server error occurred.")

    @app.get("/api/v1/health", response_model=HealthResponse,
             responses={503: {"model": ErrorResponse}})
    def health():
        try:
            check_database(settings.database_path)
        except sqlite3.Error:
            return error_response(503, "database_unavailable", "Database health check failed.")
        return HealthResponse()

    app.include_router(task_router(settings.database_path, settings.cors_origins))
    app.include_router(planning_router(settings))
    app.include_router(execution_router(settings))
    from app.responses import CHAT_PIPELINE_REVISION, response_router
    app.include_router(response_router(settings))

    @app.get('/api/v1/runtime', tags=['diagnostics'])
    def runtime():
        from app.execution.workspace import workspace_supported
        try:
            current = chat_source_fingerprint()
        except OSError:
            return error_response(503, 'source_unavailable', 'Runtime source fingerprint unavailable; restart with readable application source.')
        return {'process_id':os.getpid(), 'chat_pipeline_revision':CHAT_PIPELINE_REVISION,
                'source_snapshot_sha256':CHAT_SOURCE_SNAPSHOT, 'current_source_sha256':current,
                'restart_required':current != CHAT_SOURCE_SNAPSHOT,
                'execution_enabled':settings.execution_enabled, 'python_execution_enabled':settings.python_execution_enabled,
                'workspace_supported': workspace_supported(),
                'workspace_backend': 'windows_ntfs_handles' if os.name == 'nt' else 'posix_dir_fd',
                'agent_roles': list(AGENT_ROLES),
                'workspace_tools': ['read_text', 'write_text', 'validate_json'],
                'execution_limitations': 'New .txt/.md/.json/.csv files only; no overwrite, source-code tools, shell, application test runner or deployment. A functional web app cannot be implemented or tested with these tools.'}
    return app
