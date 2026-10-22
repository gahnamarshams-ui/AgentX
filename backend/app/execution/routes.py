"""Opt-in, local-only execution API. Existing task and plan routes stay intact."""
import asyncio
from ipaddress import ip_address
from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from app.config import Settings
from app.database import connection
from app.execution.models import ApprovalRequest, ExecutionResponse
from app.execution.security import ExecutionError
from app.execution.store import response


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    answer: str = Field(min_length=1, max_length=5000)


def execution_router(settings: Settings) -> APIRouter:
    def guard(request: Request):
        try:
            local = request.client is not None and ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        origin = request.headers.get('origin')
        if (not local or request.url.hostname not in {'localhost', '127.0.0.1', '::1'}
                or (origin is not None and origin not in settings.cors_origins)
                or request.headers.get('sec-fetch-site') == 'cross-site'
                or request.headers.get('sec-fetch-mode') in {'navigate', 'no-cors'}):
            raise ExecutionError('execution_local_only', 'Execution requires a trusted local connection and origin.', 403)
        if not settings.execution_enabled:
            # Persist the blocked request without creating a run, approval or tool.
            # Only trusted local orchestration requests may change pending state.
            if request.method == 'POST' and request.url.path.endswith('/orchestrate'):
                from app.tasks import timestamp
                from app.planning import history
                identifier = str(request.path_params.get('task_id', ''))
                with connection(settings.database_path) as db:
                    db.execute('BEGIN IMMEDIATE')
                    now = timestamp()
                    changed = db.execute("UPDATE tasks SET status='blocked', error=?, updated_at=? WHERE id=? AND status='pending' AND NOT EXISTS(SELECT 1 FROM execution_runs WHERE task_id=?)",
                        ('Workspace execution is disabled. No tools ran. Functionality: NOT TESTED.', now, identifier, identifier))
                    if changed.rowcount:
                        history(db, identifier, 'blocked', 'Workspace execution is disabled. No tools ran.', now)
            raise ExecutionError('execution_disabled', 'Workspace execution is disabled. No tools ran; operator authorization is required.', 503)

    router = APIRouter(prefix='/api/tasks', tags=['execution'], dependencies=[Depends(guard)])

    @router.post('/{task_id}/orchestrate', response_model=ExecutionResponse)
    async def orchestrate(task_id: UUID, request: Request, workflow: Literal['workspace', 'python'] = 'workspace'):
        return await request.app.state.orchestrator.start(str(task_id), workflow)

    @router.get('/{task_id}/execution', response_model=ExecutionResponse)
    async def execution(task_id: UUID):
        return await asyncio.to_thread(response, settings.database_path, str(task_id))

    @router.post('/{task_id}/clarification', response_model=ExecutionResponse)
    async def clarify(task_id: UUID, body: ClarificationRequest, request: Request):
        with connection(settings.database_path) as db:
            from app.execution.store import get_run
            from app.execution.models import ExecutionPlan
            run = get_run(db, str(task_id))
            workflow = ExecutionPlan.model_validate_json(run['plan_json']).workflow or 'workspace'
        return await request.app.state.orchestrator.start(str(task_id), workflow, answer=body.answer)

    @router.post('/{task_id}/approval', response_model=ExecutionResponse)
    async def approval(task_id: UUID, body: ApprovalRequest, request: Request):
        orchestrator = request.app.state.orchestrator
        owner = await asyncio.to_thread(orchestrator.approve, str(task_id), body)
        if owner:
            await asyncio.to_thread(orchestrator.execute, str(task_id), owner)
        return await asyncio.to_thread(response, settings.database_path, str(task_id))

    return router
