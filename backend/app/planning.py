"""Atomic planning ownership, no SQLite transaction spans an AI request."""
import asyncio
import logging
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter
from app.ai import PlanningError, generate_plan, require_key
from app.config import Settings
from app.database import connection
from app.plan_schema import TaskPlan
from app.schemas import ErrorResponse, TaskResponse
from app.tasks import task_from_row, timestamp

logger = logging.getLogger(__name__)
RECOVERY_MESSAGE = 'Planning was interrupted or expired. No plan was saved. You may retry.'


def history(db, task_id: str, status: str, message: str, now: str):
    db.execute('INSERT INTO task_history (task_id,event_type,status,message,created_at) VALUES (?,?,?,?,?)',
               (task_id, 'status_changed', status, message, now))


def recover_expired(path: Path) -> None:
    now = timestamp()
    with connection(path) as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("""SELECT t.id FROM tasks t LEFT JOIN planning_attempts a ON a.task_id=t.id
            WHERE t.status='planning' AND (a.task_id IS NULL OR a.outcome != 'active' OR a.expires_at <= ?)""", (now,)).fetchall()
        for row in rows:
            task_id = row['id']
            db.execute("UPDATE tasks SET status='failed', error=?, updated_at=? WHERE id=?", (RECOVERY_MESSAGE, now, task_id))
            db.execute("INSERT INTO planning_attempts VALUES (?,?,?,'failed') ON CONFLICT(task_id) DO UPDATE SET outcome='failed'",
                       (task_id, str(uuid4()), now))
            chat = db.execute('SELECT agents_json FROM chat_runs WHERE task_id=?', (task_id,)).fetchone()
            if chat:
                states = json.loads(chat['agents_json'])
                for state in states:
                    if state['status'] == 'running':
                        state.update(status='failed', error='Response interrupted or expired; no tools ran.', output_summary='Response interrupted or expired; no tools ran.')
                    elif state['status'] == 'pending':
                        state.update(status='skipped', output_summary='Stopped before this role ran.')
                    else:
                        continue
                    history(db, task_id, 'failed', f"[{state['role']}] {state['status']}: {state['output_summary']}", now)
                db.execute('UPDATE chat_runs SET agents_json=? WHERE task_id=?', (json.dumps(states), task_id))
            history(db, task_id, 'failed', RECOVERY_MESSAGE, now)


def claim(settings: Settings, task_id: str, clarification: str | None = None) -> tuple[str, str]:
    recover_expired(settings.database_path)
    with connection(settings.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if task is None:
            raise PlanningError('task_not_found', 'Task not found.', 404)
        root_id = task['conversation_id'] or task_id
        if db.execute("SELECT 1 FROM tasks WHERE id!=? AND (id=? OR conversation_id=?) AND status IN ('pending','planning','running')", (task_id,root_id,root_id)).fetchone():
            raise PlanningError('conversation_busy', 'Another turn is active in this conversation. Reload its saved state.', 409)
        if db.execute('SELECT 1 FROM tasks WHERE (id=? OR conversation_id=?) AND (created_at>? OR (created_at=? AND id>?))', (root_id,root_id,task['created_at'],task['created_at'],task_id)).fetchone():
            raise PlanningError('historical_turn', 'Earlier turns cannot be rerun after a follow-up is saved. Continue with a new message.', 409)
        if db.execute('SELECT 1 FROM execution_runs WHERE task_id=?', (task_id,)).fetchone():
            raise PlanningError('execution_exists', 'This task has an orchestration run. Reload execution history.', 409)
        attempt = db.execute('SELECT outcome FROM planning_attempts WHERE task_id=?', (task_id,)).fetchone()
        retry = task['status'] == 'failed' and attempt is not None and attempt['outcome'] == 'failed'
        continuing = clarification is not None and task['status'] == 'awaiting_user_input' and db.execute('SELECT 1 FROM chat_runs WHERE task_id=?', (task_id,)).fetchone()
        if clarification is not None and not continuing:
            raise PlanningError('invalid_task_state', 'Clarification requires a saved chat awaiting user input.', 409)
        if (task['status'] != 'pending' and not retry and not continuing) or task['plan'] is not None:
            raise PlanningError('invalid_task_state', 'Planning requires a pending task or a failed planning attempt. Reload task details.', 409)
        require_key(settings)  # Configuration failure leaves the existing state unchanged.
        token, now = str(uuid4()), timestamp()
        if continuing:
            db.execute('INSERT INTO chat_clarifications VALUES(?,?,?)', (task_id, clarification, now))
        answers = [r[0] for r in db.execute('SELECT answer FROM chat_clarifications WHERE task_id=? ORDER BY created_at,rowid', (task_id,))]
        if len(json.dumps(answers)) > 96000:
            raise PlanningError('conversation_too_large', 'Saved clarifications exceed the context limit. No new answer was saved.', 413)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=settings.openai_timeout_seconds + 30)).isoformat(timespec='microseconds')
        db.execute("INSERT INTO planning_attempts VALUES (?,?,?,'active') ON CONFLICT(task_id) DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at,outcome='active'",
                   (task_id, token, expires))
        db.execute("UPDATE tasks SET status='planning', error=NULL, updated_at=? WHERE id=?", (now, task_id))
        history(db, task_id, 'planning', 'AI planning requested. No task steps are being executed.', now)
        return token, (json.dumps({'original_request':task['description'], 'clarification_answers':answers}) if answers else task['description'])


def finish(path: Path, task_id: str, token: str, *, plan: TaskPlan | None = None, error: str | None = None) -> TaskResponse:
    if (plan is None) == (error is None):
        raise ValueError('Exactly one plan or safe error is required')
    if plan is not None:
        plan = TaskPlan.model_validate(plan.model_dump())
    now = timestamp()
    with connection(path) as db:
        db.execute('BEGIN IMMEDIATE')
        owner = db.execute("""SELECT 1 FROM planning_attempts a JOIN tasks t ON t.id=a.task_id
            WHERE a.task_id=? AND a.token=? AND a.outcome='active' AND a.expires_at>? AND t.status='planning'""",
            (task_id, token, now)).fetchone()
        if owner is None:
            raise PlanningError('planning_expired', 'Planning ownership expired or changed. Reload the saved task.', 409)
        status = ('awaiting_user_input' if plan.clarification_needed else 'awaiting_approval') if plan is not None else 'failed'
        db.execute('UPDATE tasks SET status=?, plan=?, error=?, updated_at=? WHERE id=?',
                   (status, plan.model_dump_json() if plan is not None else None, error, now, task_id))
        db.execute('UPDATE planning_attempts SET outcome=? WHERE task_id=? AND token=?',
                   ('succeeded' if plan is not None else 'failed', task_id, token))
        history(db, task_id, status, 'Validated AI plan saved for review. No steps have been executed.' if plan is not None else error, now)
        return task_from_row(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())


def try_fail(path: Path, task_id: str, token: str, message: str) -> None:
    try:
        finish(path, task_id, token, error=message)
    except (sqlite3.Error, PlanningError):
        # Expiring ownership is the durable fallback if storage is unavailable.
        logger.error('Planning failure could not be recorded immediately; expiry recovery will retry.')


async def recover_loop(path: Path):
    while True:
        await asyncio.sleep(5)
        try:
            await asyncio.to_thread(recover_expired, path)
            from app.execution.store import recover
            await asyncio.to_thread(recover, path)
            from app.execution.python_workflow import mark_stale_python_jobs
            await asyncio.to_thread(mark_stale_python_jobs, path)
        except sqlite3.Error:
            logger.error('Planning recovery storage unavailable; will retry.')


def planning_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix='/api/tasks', tags=['planning'])

    @router.post('/{task_id}/plan', response_model=TaskResponse,
                 responses={code: {'model': ErrorResponse} for code in [404, 409, 422, 429, 502, 503, 504]})
    async def plan_task(task_id: UUID):
        identifier = str(task_id)
        token, description = await asyncio.to_thread(claim, settings, identifier)
        try:
            plan = await generate_plan(settings, description)
            return await asyncio.to_thread(finish, settings.database_path, identifier, token, plan=plan)
        except PlanningError as exc:
            await asyncio.to_thread(try_fail, settings.database_path, identifier, token, exc.message)
            raise
        except asyncio.CancelledError:
            # Client disconnect/shutdown must never create a successful plan.
            await asyncio.shield(asyncio.to_thread(try_fail, settings.database_path, identifier, token, RECOVERY_MESSAGE))
            raise
        except sqlite3.Error:
            await asyncio.to_thread(try_fail, settings.database_path, identifier, token, 'Plan could not be saved because storage was unavailable. You may retry.')
            raise
        except Exception:
            await asyncio.to_thread(try_fail, settings.database_path, identifier, token, 'Planning failed unexpectedly. No plan was saved. You may retry.')
            raise PlanningError('planning_failed', 'Planning failed unexpectedly. Reload the task and retry.', 502) from None

    return router
