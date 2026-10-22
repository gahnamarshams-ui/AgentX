"""Execution records share the existing SQLite connection and task history."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from app.database import connection
from app.execution.models import ExecutionPlan, ExecutionResponse, StepState
from app.execution.security import ExecutionError
from app.planning import history
from app.tasks import timestamp
from app.execution.progress import agent_states

TRANSITIONS = {
    'planning': frozenset({'awaiting_user_input', 'awaiting_approval', 'failed'}),
    'awaiting_user_input': frozenset({'planning', 'failed'}),
    'awaiting_approval': frozenset({'executing', 'cancelled', 'failed'}),
    'executing': frozenset({'verifying', 'failed'}),
    'verifying': frozenset({'completed', 'failed'}),
    'completed': frozenset(), 'failed': frozenset(), 'cancelled': frozenset(),
}

INTERRUPTED = 'Execution was interrupted or expired. Outputs may be partial; inspect the workspace. Automatic replay is disabled.'


def expiry(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='microseconds')


def digest(plan: ExecutionPlan, workspace_id: str) -> str:
    data = plan.model_dump()
    if plan.workflow not in {'five_agent', 'python'}: data.pop('workflow')  # Preserve file-plan digests.
    value = json.dumps({'plan': data, 'workspace': workspace_id}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()


def get_run(db, task_id: str):
    row = db.execute('SELECT * FROM execution_runs WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        exists = db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone()
        raise ExecutionError('execution_not_found' if exists else 'task_not_found',
                             'No orchestration run exists for this task.' if exists else 'Task not found.', 404)
    return row


def load_plan(db, run, workspace_id: str) -> ExecutionPlan:
    try:
        plan = ExecutionPlan.model_validate_json(run['plan_json'])
        rows = db.execute('SELECT * FROM execution_steps WHERE task_id=? ORDER BY number', (run['task_id'],)).fetchall()
        coherent = len(rows) == len(plan.steps) and all(
            r['number'] == s.number and json.loads(r['tool_json']) == s.tool.model_dump()
            and json.loads(r['dependencies']) == s.dependencies for r, s in zip(rows, plan.steps))
        if run['workspace_id'] != workspace_id or digest(plan, workspace_id) != run['plan_digest'] or not coherent:
            raise ValueError('Changed execution specification')
        return plan
    except (ValueError, TypeError):
        raise ExecutionError('execution_changed', 'Execution specification or workspace changed. Start a new task.', 409) from None


def require_owner(db, task_id: str, owner: str):
    run = get_run(db, task_id)
    if (run['owner'] != owner or run['phase'] not in {'executing', 'verifying'}
            or run['expires_at'] is None or run['expires_at'] <= timestamp()):
        raise ExecutionError('execution_expired', 'Execution ownership expired or changed. Reload task history.', 409)
    return run


def transition(db, task_id: str, phase: str, message: str, *, lease: int | None = None, error: str | None = None):
    if not db.in_transaction:
        raise RuntimeError('Execution transitions require an explicit transaction')
    run = get_run(db, task_id)
    if phase not in TRANSITIONS[run['phase']]:
        raise ExecutionError('invalid_transition', 'Execution cannot make this state transition.', 409)
    if phase == 'executing':
        approved = db.execute("SELECT 1 FROM execution_approvals WHERE task_id=? AND plan_digest=? AND decision='approve'",
                              (task_id, run['plan_digest'])).fetchone()
        if not approved:
            raise ExecutionError('approval_required', 'Explicit approval is required before any tool execution.', 409)
    if phase == 'completed':
        steps = db.execute('SELECT status,result,verification FROM execution_steps WHERE task_id=?', (task_id,)).fetchall()
        if not steps or any(row['status'] != 'verified' or not row['result'] or not row['verification']
                            or json.loads(row['verification']).get('passed') is not True for row in steps):
            raise ExecutionError('verification_required', 'Every step must have verified output before completion.', 409)
    status = 'running' if phase in {'executing', 'verifying'} else phase
    now = timestamp()
    db.execute('UPDATE execution_runs SET phase=?,expires_at=?,error=?,updated_at=? WHERE task_id=?',
               (phase, expiry(lease) if lease else None, error, now, task_id))
    db.execute('UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?', (status, error, now, task_id))
    history(db, task_id, status, message, now)


def response(path: Path, task_id: str) -> ExecutionResponse:
    with connection(path) as db:
        # A consistent snapshot across the run, approval and steps.
        db.execute('BEGIN')
        run = get_run(db, task_id)
        approval = db.execute('SELECT decision FROM execution_approvals WHERE task_id=?', (task_id,)).fetchone()
        steps = []
        for row in db.execute('SELECT * FROM execution_steps WHERE task_id=? ORDER BY number', (task_id,)):
            steps.append(StepState(number=row['number'], dependencies=json.loads(row['dependencies']),
                tool=json.loads(row['tool_json']), status=row['status'], attempts=row['attempts'],
                result=json.loads(row['result']) if row['result'] else None,
                verification=json.loads(row['verification']) if row['verification'] else None, error=row['error']))
        review_started = db.execute("SELECT 1 FROM task_history WHERE task_id=? AND (message LIKE '[Review / Verifier]%' OR message LIKE '[Verifier]%')", (task_id,)).fetchone() is not None
        return ExecutionResponse(run_id=run['id'], task_id=task_id, phase=run['phase'],
            plan_digest=run['plan_digest'], approval_required=bool(run['approval_required']),
            approval_decision=approval['decision'] if approval else None, steps=steps, error=run['error'],
            agents=agent_states(run, steps, review_started, approval['decision'] if approval else None))


def recover(path: Path):
    with connection(path) as db:
        db.execute('BEGIN IMMEDIATE')
        now = timestamp()
        runs = db.execute("SELECT * FROM execution_runs WHERE phase IN ('planning','executing','verifying')").fetchall()
        for run in runs:
            task_id = run['task_id']
            task = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if run['phase'] == 'planning':
                if task['status'] == 'failed':
                    db.execute("UPDATE execution_runs SET phase='failed',error=?,expires_at=NULL,updated_at=? WHERE task_id=?",
                               ('Planning did not produce an executable plan.', now, task_id))
                continue  # Existing planning recovery owns planning leases/history.
            if run['expires_at'] is None or run['expires_at'] <= now:
                transition(db, task_id, 'failed', '[Orchestrator] ' + INTERRUPTED, error=INTERRUPTED)
                db.execute("UPDATE execution_steps SET status='failed',error=? WHERE task_id=? AND status='executing'", (INTERRUPTED, task_id))
