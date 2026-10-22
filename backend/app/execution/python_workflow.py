"""Ownership-fenced Python jobs. All runtime I/O occurs outside DB transactions."""
import json
from uuid import uuid4
from app.database import connection
from app.execution.python_models import PythonEvidence, verify_python
from app.execution.sandbox import config_hash
from app.execution.security import ExecutionError, check_secrets
from app.execution.store import transition
from app.planning import history
from app.tasks import timestamp


def mark_stale_python_jobs(path):
    with connection(path) as db:
        db.execute("""UPDATE python_jobs SET state='uncertain',updated_at=?
            WHERE state IN ('claimed','submitted','verifying') AND task_id IN
            (SELECT p.task_id FROM python_jobs p JOIN execution_runs r ON r.task_id=p.task_id
             WHERE r.phase NOT IN ('executing','verifying') OR r.owner!=p.owner
             OR r.expires_at IS NULL OR r.expires_at<=?)""", (timestamp(), timestamp()))


def execute_python(orchestrator, task_id, owner, plan):
    o = orchestrator
    call = plan.steps[0].tool
    job_id = uuid4().hex
    submitted = False
    claimed = False
    try:
        with connection(o.path) as db:
            db.execute('BEGIN IMMEDIATE')
            current_plan = o.authorize(db, task_id, owner)
            if current_plan != plan: raise ExecutionError('execution_changed', 'Saved Python plan changed.')
            step = db.execute('SELECT status,attempts FROM execution_steps WHERE task_id=? AND number=1', (task_id,)).fetchone()
            if step['status'] != 'pending' or step['attempts'] != 0:
                raise ExecutionError('sandbox_replay', 'Python source is never automatically replayed.')
            if db.execute("SELECT 1 FROM python_jobs WHERE state IN ('claimed','submitted','verifying','uncertain')").fetchone():
                raise ExecutionError('sandbox_busy', 'Sandbox is busy or cleanup is uncertain.')
            saved_digest = db.execute('SELECT plan_digest FROM execution_runs WHERE task_id=?', (task_id,)).fetchone()[0]
            db.execute("INSERT INTO python_jobs VALUES (?,?,?,'claimed',NULL,?,?)", (task_id, job_id, owner, timestamp(), timestamp()))
            db.execute("UPDATE execution_steps SET status='executing',attempts=1 WHERE task_id=? AND number=1", (task_id,))
            history(db, task_id, 'running', '[Executor] Sandbox job claimed; no runtime started yet.', timestamp())
        claimed = True
        o.sandbox.preflight(call.runtime)
        with connection(o.path) as db:
            db.execute('BEGIN IMMEDIATE')
            o.authorize(db, task_id, owner)
            db.execute("UPDATE python_jobs SET state='submitted',updated_at=? WHERE task_id=? AND owner=?", (timestamp(), task_id, owner))
        # Persist intent before submission. An exception after this point is uncertain,
        # never a reason to resubmit. The supervisor rejects duplicate job identities.
        submitted = True
        actual = PythonEvidence.model_validate(o.sandbox.run(job_id, saved_digest, call).model_dump())
        check_secrets(o.settings, actual.model_dump_json())
        with connection(o.path) as db:
            db.execute('BEGIN IMMEDIATE')
            o.authorize(db, task_id, owner)
            db.execute("UPDATE python_jobs SET state='verifying',evidence=?,updated_at=? WHERE task_id=? AND owner=?", (actual.model_dump_json(), timestamp(), task_id, owner))
            db.execute("UPDATE execution_steps SET status='executed',result=? WHERE task_id=? AND number=1", (actual.model_dump_json(), task_id))
            transition(db, task_id, 'verifying', '[Verifier] Checking the independent supervisor receipt and approved test expectations.', lease=o.settings.execution_lease_seconds)
        # Independent receipt fetch: not candidate output, and not the Executor's copy.
        trusted = o.sandbox.receipt(job_id)
        cutoffs = o.sandbox.deadline_receipts(actual)
        passed, checks = verify_python(call, actual, trusted, job_id, saved_digest, config_hash(call.runtime), cutoffs)
        if not passed:
            with connection(o.path) as db:
                db.execute('BEGIN IMMEDIATE')
                o.authorize(db, task_id, owner)
                db.execute("UPDATE execution_steps SET status='failed',verification=?,error=? WHERE task_id=? AND number=1",
                    (json.dumps({'passed': False, 'checks': checks}), 'Sandbox evidence or approved tests failed.', task_id))
            raise ExecutionError('sandbox_verification_failed', 'Sandbox execution evidence or approved tests failed verification.')
        with connection(o.path) as db:
            db.execute('BEGIN IMMEDIATE')
            o.authorize(db, task_id, owner)
            row = db.execute('SELECT result FROM execution_steps WHERE task_id=? AND number=1', (task_id,)).fetchone()
            if PythonEvidence.model_validate_json(row['result']) != actual:
                raise ExecutionError('sandbox_evidence_changed', 'Saved sandbox evidence changed.')
            db.execute("UPDATE execution_steps SET status='verified',verification=? WHERE task_id=? AND number=1", (json.dumps({'passed': True, 'checks': checks}), task_id))
            db.execute("UPDATE python_jobs SET state='completed',updated_at=? WHERE task_id=? AND owner=?", (timestamp(), task_id, owner))
            db.execute('UPDATE tasks SET final_result=? WHERE id=?', (f'Approved Python cases passed ({len(call.cases)}). Runtime receipt, source/tests, limits and cleanup verified. Finite tests do not prove general correctness.', task_id))
            transition(db, task_id, 'completed', '[Orchestrator] Five-role Python workflow completed with verified sandbox evidence.')
    except BaseException:
        # Cleanup is permitted even if execution is now disabled or ownership changed.
        # It can only stop a recorded job; never create/start containers or replay code.
        cleaned = not submitted
        if submitted:
            try: cleaned = o.sandbox.stop(job_id)
            except Exception: cleaned = False
        if claimed:
            with connection(o.path) as db:
                db.execute('UPDATE python_jobs SET state=?,updated_at=? WHERE task_id=? AND owner=?',
                           ('failed' if cleaned else 'uncertain', timestamp(), task_id, owner))
        o.fail(task_id, owner, 'Python sandbox failed or ownership changed. Source was not replayed; unconfirmed cleanup blocks further jobs.')
        raise ExecutionError('sandbox_failed', 'Python sandbox did not complete verified execution. Inspect saved evidence; do not replay.', 502) from None


def recover_python_jobs(settings, client=None):
    """Only reconcile stale intent with STOP; never submit stored source on restart."""
    from app.execution.sandbox import SupervisorClient
    with connection(settings.database_path) as db:
        rows = db.execute("""SELECT p.* FROM python_jobs p JOIN execution_runs r ON r.task_id=p.task_id
            WHERE p.state IN ('claimed','submitted','verifying','uncertain')
            AND (r.phase NOT IN ('executing','verifying') OR r.owner!=p.owner OR r.expires_at IS NULL OR r.expires_at<=?)""", (timestamp(),)).fetchall()
    for row in rows:
        cleaned = row['state'] == 'claimed'  # No submission intent was committed.
        if not cleaned and (client is not None or settings.sandbox_supervisor_socket is not None):
            try: cleaned = (client or SupervisorClient(settings)).stop(row['job_id'])
            except Exception: cleaned = False
        with connection(settings.database_path) as db:
            db.execute("UPDATE python_jobs SET state=?,updated_at=? WHERE task_id=? AND owner=? AND state IN ('claimed','submitted','verifying','uncertain')",
                       ('failed' if cleaned else 'uncertain', timestamp(), row['task_id'], row['owner']))
