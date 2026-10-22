"""SQLite task persistence. No model calls, execution, or public status mutations."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from ipaddress import ip_address

from app.database import connection
from app.task_intent import requires_execution
from app.schemas import ConversationUpdate, ErrorResponse, HistoryResponse, TaskCreate, TaskListResponse, TaskResponse


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def task_from_row(row) -> TaskResponse:
    data = dict(row)
    data.pop('execution_required', None)  # Internal completion policy, not client authority.
    data['archived'] = bool(data.get('archived', False))
    data['plan'] = json.loads(data['plan']) if data['plan'] is not None else None
    return TaskResponse.model_validate(data)


def create_task(path: Path, body: TaskCreate) -> TaskResponse:
    task_id, now = str(uuid4()), timestamp()
    with connection(path) as db:
        db.execute('BEGIN IMMEDIATE')
        root_id = None
        if body.conversation_id:
            parent = db.execute('SELECT * FROM tasks WHERE id=?', (str(body.conversation_id),)).fetchone()
            if parent is None: raise HTTPException(404, 'Conversation not found.')
            root_id = parent['conversation_id'] or parent['id']
            root = db.execute('SELECT * FROM tasks WHERE id=?', (root_id,)).fetchone()
            if root['archived']: raise HTTPException(409, 'Restore this archived conversation before sending a message.')
            group = db.execute('SELECT id,status,plan FROM tasks WHERE id=? OR conversation_id=?', (root_id, root_id)).fetchall()
            if any(t['status'] in {'pending','planning','running','awaiting_approval'} for t in group):
                raise HTTPException(409, 'Wait for the current turn to finish or retry it before sending another message.')
            if any(t['plan'] is not None for t in group) or not db.execute('SELECT 1 FROM chat_runs WHERE task_id=?', (root_id,)).fetchone():
                raise HTTPException(409, 'Follow-ups require a text-chat conversation. Start a New Chat for this task.')
            if db.execute('SELECT 1 FROM execution_runs WHERE task_id IN (SELECT id FROM tasks WHERE id=? OR conversation_id=?)', (root_id, root_id)).fetchone():
                raise HTTPException(409, 'Workspace execution tasks cannot be used as text-chat context.')
        db.execute('INSERT INTO tasks (id, description, status, created_at, updated_at,conversation_id) VALUES (?, ?, ?, ?, ?, ?)',
                   (task_id, body.description, 'pending', now, now, root_id))
        inherited = root_id and db.execute('SELECT 1 FROM tasks WHERE (id=? OR conversation_id=?) AND execution_required=1', (root_id, root_id)).fetchone()
        if inherited or requires_execution(body.description):
            db.execute('UPDATE tasks SET execution_required=1 WHERE id=?', (task_id,))
        db.execute('INSERT INTO task_history (task_id, event_type, status, message, created_at) VALUES (?, ?, ?, ?, ?)',
                   (task_id, 'task_created', 'pending', 'Task saved. Execution has not started.', now))
        task = task_from_row(db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone())
    return task


def cancel_pending_task(path: Path, task_id: str) -> None:
    """Internal persistence operation only; no cancellation endpoint in this phase.

    Only pending -> cancelled is supported until real orchestration exists.
    Lock before reading to prevent conflicting transitions and duplicate events.
    """
    with connection(path) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise LookupError('Task not found')
        if row['status'] != 'pending':
            raise ValueError('Only pending tasks can be cancelled')
        now = timestamp()
        db.execute('UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?', ('cancelled', now, task_id))
        db.execute('INSERT INTO task_history (task_id, event_type, status, message, created_at) VALUES (?, ?, ?, ?, ?)',
                   (task_id, 'status_changed', 'cancelled', 'Pending task cancelled before execution.', now))


def task_router(path: Path, cors_origins=None) -> APIRouter:
    router = APIRouter(prefix='/api/tasks', tags=['tasks'], responses={
        422: {'model': ErrorResponse}, 503: {'model': ErrorResponse}, 404: {'model': ErrorResponse}})

    @router.post('', response_model=TaskResponse, status_code=201)
    def create(body: TaskCreate):
        return create_task(path, body)

    @router.get('', response_model=TaskListResponse)
    def list_recent(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                    q: str = Query('', max_length=500), archived: bool | None = None, conversations_only: bool = False):
        with connection(path) as db:
            where, args = [], []
            if conversations_only: where.append('t.conversation_id IS NULL')
            if archived is not None:
                where.append('t.archived=?'); args.append(int(archived))
            if q.strip():
                # instr gives literal, parameterized search (no wildcard surprises).
                where.append("(instr(lower(coalesce(t.title,'') || ' ' || t.description || ' ' || coalesce(t.plan,'') || ' ' || coalesce(t.final_result,'') || ' ' || coalesce(t.error,'')),lower(?))>0 OR EXISTS (SELECT 1 FROM task_history h WHERE h.task_id=t.id AND instr(lower(h.message),lower(?))>0) OR EXISTS (SELECT 1 FROM tasks c WHERE c.conversation_id=t.id AND (instr(lower(c.description || ' ' || coalesce(c.final_result,'') || ' ' || coalesce(c.error,'')),lower(?))>0 OR EXISTS (SELECT 1 FROM task_history h WHERE h.task_id=c.id AND instr(lower(h.message),lower(?))>0))))")
                args.extend([q.strip(), q.strip()])
                args.extend([q.strip(), q.strip()])
            sql = "SELECT t.*, CASE WHEN EXISTS(SELECT 1 FROM chat_runs c WHERE c.task_id=t.id) THEN 'chat' ELSE NULL END AS response_mode FROM tasks t" + (' WHERE ' + ' AND '.join(where) if where else '')
            rows = db.execute(sql + ' ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        return TaskListResponse(tasks=[task_from_row(row) for row in rows], limit=limit, offset=offset)

    @router.get('/{task_id}/turns', response_model=TaskListResponse)
    def turns(task_id: UUID):
        with connection(path) as db:
            parent = db.execute('SELECT id,conversation_id FROM tasks WHERE id=?', (str(task_id),)).fetchone()
            if parent is None: raise HTTPException(404, 'Conversation not found.')
            root_id = parent['conversation_id'] or parent['id']
            rows = db.execute("SELECT t.*, CASE WHEN EXISTS(SELECT 1 FROM chat_runs c WHERE c.task_id=t.id) THEN 'chat' ELSE NULL END AS response_mode FROM tasks t WHERE t.id=? OR t.conversation_id=? ORDER BY t.created_at,t.id", (root_id,root_id)).fetchall()
        return TaskListResponse(tasks=[task_from_row(row) for row in rows], limit=len(rows), offset=0)

    @router.get('/{task_id}', response_model=TaskResponse)
    def detail(task_id: UUID):
        with connection(path) as db:
            row = db.execute("SELECT t.*, CASE WHEN EXISTS(SELECT 1 FROM chat_runs c WHERE c.task_id=t.id) THEN 'chat' ELSE NULL END AS response_mode FROM tasks t WHERE id=?", (str(task_id),)).fetchone()
        if row is None:
            raise HTTPException(404, 'Task not found.')
        return task_from_row(row)

    @router.get('/{task_id}/history', response_model=HistoryResponse)
    def history(task_id: UUID):
        with connection(path) as db:
            if db.execute('SELECT 1 FROM tasks WHERE id = ?', (str(task_id),)).fetchone() is None:
                raise HTTPException(404, 'Task not found.')
            rows = db.execute('SELECT * FROM task_history WHERE task_id = ? ORDER BY created_at ASC, id ASC',
                              (str(task_id),)).fetchall()
        return HistoryResponse(events=[dict(row) for row in rows])

    def local_mutation(request: Request):
        try:
            local = request.client is not None and ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        allowed = cors_origins or ['http://localhost:5173', 'http://127.0.0.1:5173']
        origin = request.headers.get('origin')
        if (not local or request.url.hostname not in {'localhost', '127.0.0.1', '::1'}
                or (origin is not None and origin not in allowed)
                or request.headers.get('sec-fetch-site') == 'cross-site'):
            raise HTTPException(403, 'Conversation changes require a trusted local connection and origin.')

    @router.post('/{task_id}/conversation', response_model=TaskResponse)
    def update_conversation(task_id: UUID, body: ConversationUpdate, request: Request):
        local_mutation(request)
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(422, 'Provide a title or archive state.')
        with connection(path) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM tasks WHERE id=?', (str(task_id),)).fetchone() is None:
                raise HTTPException(404, 'Task not found.')
            for key, value in changes.items():
                db.execute(f'UPDATE tasks SET {key}=? WHERE id=?', (value, str(task_id)))
            return task_from_row(db.execute("SELECT t.*, CASE WHEN EXISTS(SELECT 1 FROM chat_runs c WHERE c.task_id=t.id) THEN 'chat' ELSE NULL END AS response_mode FROM tasks t WHERE id=?", (str(task_id),)).fetchone())

    @router.delete('/{task_id}')
    def delete_conversation(task_id: UUID, request: Request):
        local_mutation(request)
        identifier = str(task_id)
        with connection(path) as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT status,conversation_id FROM tasks WHERE id=?', (identifier,)).fetchone()
            if task is None:
                raise HTTPException(404, 'Task not found.')
            if task['conversation_id']:
                raise HTTPException(409, 'Delete the conversation through its first task, not an individual turn.')
            members = db.execute('SELECT id,status FROM tasks WHERE id=? OR conversation_id=? ORDER BY conversation_id IS NULL', (identifier,identifier)).fetchall()
            for member in members:
                run = db.execute('SELECT phase FROM execution_runs WHERE task_id=?', (member['id'],)).fetchone()
                if member['status'] in {'planning', 'running'} or (run and run['phase'] in {'planning', 'executing', 'verifying'}):
                    raise HTTPException(409, 'An active task cannot be deleted. Wait for it to stop.')
                job = db.execute('SELECT state FROM python_jobs WHERE task_id=?', (member['id'],)).fetchone()
                if job and job['state'] in {'claimed', 'submitted', 'verifying', 'uncertain'}:
                    raise HTTPException(409, 'Sandbox cleanup must be confirmed before its conversation can be deleted.')
            for member in members:  # Children first, then the root; all rows share this transaction.
                for table in ('chat_clarifications', 'chat_runs', 'python_jobs', 'execution_clarifications', 'execution_approvals', 'execution_steps', 'execution_runs', 'planning_attempts', 'task_history'):
                    db.execute(f'DELETE FROM {table} WHERE task_id=?', (member['id'],))
                db.execute('DELETE FROM tasks WHERE id=?', (member['id'],))
        return {'deleted': True, 'id': identifier}

    @router.get('/{task_id}/activity')
    def activity(task_id: UUID):
        # Read durable evidence only; works even when execution is disabled.
        from app.execution.store import response
        from app.execution.models import AgentState
        identifier = str(task_id)
        with connection(path) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Task not found.')
            if db.execute('SELECT 1 FROM execution_runs WHERE task_id=?', (identifier,)).fetchone():
                return response(path, identifier)
            chat = db.execute('SELECT agents_json FROM chat_runs WHERE task_id=?', (identifier,)).fetchone()
            if chat:
                states = [AgentState.model_validate(a) for a in json.loads(chat['agents_json'])]
                if row['status'] == 'failed':
                    for state in states:
                        if state.status == 'running': state.status, state.error = 'failed', row['error']
                        elif state.status == 'pending': state.status = 'skipped'
                return {'task_id': identifier, 'phase': row['status'], 'plan_digest': None, 'agents': states}
            attempt = db.execute('SELECT * FROM planning_attempts WHERE task_id=?', (identifier,)).fetchone()
        task = task_from_row(row)
        planning = task.status == 'planning'
        result = []
        for role in ('Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier'):
            state, output = 'skipped', 'Not invoked by the proposal-only /plan workflow.'
            if role == 'Planner':
                state = 'succeeded' if task.plan else 'running' if planning else 'failed' if attempt else 'pending'
                output = 'Validated proposal saved; no tools executed.' if task.plan else 'No validated proposal saved.'
            elif role == 'Safety':
                state, output = 'pending', 'Proposal only; no executable plan digest or tool authorization exists.'
            result.append(AgentState(role=role, status=state, attempts=1 if role == 'Planner' and attempt else 0,
                input_summary='Saved task description and planning attempt.' if role == 'Planner' else 'No execution requested.',
                output_summary=output, error=task.error if role == 'Planner' and state == 'failed' else None))
        return {'task_id': identifier, 'phase': task.status.value, 'plan_digest': None, 'agents': result}

    return router
