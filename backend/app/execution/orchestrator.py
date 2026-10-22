"""Central lifecycle owner; models propose, policy gates, tools act, verifier checks."""
import asyncio
import json
import logging
import sqlite3
from uuid import uuid4
from app.ai import PlanningError
from app.config import Settings
from app.database import connection
from app.execution.agents import OpenAIPlanner, OutputVerifier, PolicySafety, WorkspaceExecutor, PlannerAgent, SafetyAgent, ExecutorAgent, VerifierAgent
from app.execution.models import ApprovalRequest, ExecutionPlan, ToolResult, Verification
from app.execution.security import ExecutionError, ToolFailure, check_secrets
from app.execution.store import digest, expiry, get_run, load_plan, require_owner, response, transition
from app.execution.workspace import Workspace
from app.planning import claim, history, try_fail
from app.tasks import timestamp

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings, *, planner: PlannerAgent | None = None,
                 safety: SafetyAgent | None = None, executor: ExecutorAgent | None = None,
                 verifier: VerifierAgent | None = None, sandbox=None):
        self.settings, self.path = settings, settings.database_path
        self.workspace = Workspace(settings)
        self.planner = planner or OpenAIPlanner(settings)
        self.safety = safety or PolicySafety(settings)
        self.executor = executor or WorkspaceExecutor(self.workspace)
        self.verifier = verifier or OutputVerifier(self.workspace)
        from app.execution.sandbox import SupervisorClient
        self.sandbox = sandbox or SupervisorClient(settings)

    def begin(self, task_id: str) -> tuple[str, str]:
        with connection(self.path) as db:
            if db.execute('SELECT 1 FROM execution_runs WHERE task_id=?', (task_id,)).fetchone():
                raise ExecutionError('execution_exists', 'This task already has an orchestration run. Reload its execution history.', 409)
            task = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task is not None and task['status'] != 'pending':
                raise ExecutionError('invalid_task_state', 'Orchestration requires a new pending task.', 409)
        owner, description = claim(self.settings, task_id)
        try:
            with connection(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                now = timestamp()
                db.execute('''INSERT INTO execution_runs
                    (task_id,id,phase,workspace_id,owner,expires_at,created_at,updated_at)
                    VALUES (?,?,'planning',?,?,?,?,?)''', (task_id, str(uuid4()), self.workspace.identity, owner,
                    expiry(self.settings.openai_timeout_seconds + 30), now, now))
                history(db, task_id, 'planning', '[Planner] Preparing a bounded workspace execution plan.', now)
            return owner, description
        except Exception:
            try_fail(self.path, task_id, owner, 'Orchestration could not be initialized. No tools were executed.')
            raise

    def save_plan(self, task_id: str, owner: str, plan: ExecutionPlan):
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            run = get_run(db, task_id)
            attempt = db.execute("SELECT * FROM planning_attempts WHERE task_id=?", (task_id,)).fetchone()
            task = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if (run['owner'] != owner or run['phase'] != 'planning' or task['status'] != 'planning'
                    or attempt['token'] != owner or attempt['outcome'] != 'active' or attempt['expires_at'] <= timestamp()):
                raise ExecutionError('planning_expired', 'Planning ownership expired. Start a new task.', 409)
            db.execute('UPDATE execution_runs SET plan_json=?,plan_digest=?,approval_required=? WHERE task_id=?',
                       (plan.model_dump_json(), None if plan.clarification_needed else digest(plan, self.workspace.identity), 0 if plan.clarification_needed else 1, task_id))
            if plan.clarification_needed:
                db.execute('UPDATE tasks SET plan=?,final_result=? WHERE id=?', (plan.display_plan().model_dump_json(), '\n\n'.join(plan.clarification_questions), task_id))
                db.execute("UPDATE planning_attempts SET outcome='succeeded' WHERE task_id=? AND token=?", (task_id, owner))
                transition(db, task_id, 'awaiting_user_input', '[Planner] Clarification required. No tools or final artifacts generated.')
                return
            for step in plan.steps:
                db.execute('''INSERT INTO execution_steps (task_id,number,dependencies,tool_json,status)
                    VALUES (?,?,?,?,'pending')''', (task_id, step.number, json.dumps(step.dependencies), step.tool.model_dump_json()))
            db.execute('UPDATE tasks SET plan=? WHERE id=?', (plan.display_plan().model_dump_json(), task_id))
            db.execute("UPDATE planning_attempts SET outcome='succeeded' WHERE task_id=? AND token=?", (task_id, owner))
            transition(db, task_id, 'awaiting_approval',
                       '[Safety] Explicit approval required for the exact saved plan. No tools were executed.')

    def planning_failed(self, task_id: str, owner: str, message: str):
        try_fail(self.path, task_id, owner, message)
        try:
            with connection(self.path) as db:
                db.execute("UPDATE execution_runs SET phase='failed',error=?,expires_at=NULL,updated_at=? WHERE task_id=? AND owner=? AND phase='planning'",
                           (message, timestamp(), task_id, owner))
        except sqlite3.Error:
            logger.error('Orchestration planning recovery will retry when storage is available.')

    def resume(self, task_id: str, answer: str):
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            run = get_run(db, task_id)
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if run['phase'] != 'awaiting_user_input' or task['status'] != 'awaiting_user_input':
                raise ExecutionError('clarification_conflict', 'Only a task awaiting input can accept clarification.', 409)
            check_secrets(self.settings, answer)
            owner = str(uuid4())
            lease = expiry(self.settings.openai_timeout_seconds + 30)
            db.execute('INSERT INTO execution_clarifications VALUES (?,?,?)', (task_id, answer, timestamp()))
            db.execute("UPDATE planning_attempts SET token=?,outcome='active',expires_at=? WHERE task_id=?", (owner, lease, task_id))
            db.execute('UPDATE execution_runs SET owner=? WHERE task_id=?', (owner, task_id))
            transition(db, task_id, 'planning', '[Planner] Continuing the original task with saved clarification.', lease=int(self.settings.openai_timeout_seconds + 30))
            db.execute('UPDATE tasks SET final_result=NULL WHERE id=?', (task_id,))
            return owner, task['description']

    async def start(self, task_id: str, workflow: str = 'workspace', answer: str | None = None):
        if not self.settings.execution_enabled:
            raise ExecutionError('execution_disabled', 'Workspace execution is disabled.', 503)
        if workflow == 'python':
            from app.execution.sandbox import configured_runtime
            configured_runtime(self.settings)
        owner, description = await asyncio.to_thread(self.resume, task_id, answer) if answer is not None else await asyncio.to_thread(self.begin, task_id)
        try:
            with connection(self.path) as db:
                answers = [r['answer'] for r in db.execute('SELECT answer FROM execution_clarifications WHERE task_id=? ORDER BY rowid', (task_id,))]
            context = [{'user': description, 'assistant': None, 'status': 'awaiting_user_input'}] if answers else []
            context += [{'user': value, 'assistant': None, 'status': 'awaiting_user_input'} for value in answers[:-1]]
            planning_input = json.dumps({'original_request': description, 'clarification_answers': answers}) if answers else description
            proposed = await (self.planner.plan_python(planning_input) if workflow == 'python' else self.planner.plan(planning_input))
            plan = ExecutionPlan.model_validate(proposed.model_dump())
            from app.responses import ResponseSafety
            reviewed, _ = ResponseSafety(self.settings).review_conflicts(answers[-1] if answers else description, plan.display_plan(), context)
            plan = ExecutionPlan.model_validate({**plan.model_dump(), 'clarification_needed': reviewed.clarification_needed, 'clarification_questions': reviewed.clarification_questions})
            if (plan.workflow == 'python') != (workflow == 'python'):
                raise ExecutionError('invalid_workflow', 'Python requires an explicit Python workflow request.', 422)
            if plan.clarification_needed:
                check_secrets(self.settings, plan.model_dump_json())
            else:
                self.safety.assess(plan)
            await asyncio.to_thread(self.save_plan, task_id, owner, plan)
        except (PlanningError, ExecutionError) as exc:
            await asyncio.to_thread(self.planning_failed, task_id, owner, exc.message)
            raise
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self.planning_failed, task_id, owner, 'Planning was interrupted. No tools were executed.'))
            raise
        except sqlite3.Error:
            await asyncio.to_thread(self.planning_failed, task_id, owner, 'Execution plan could not be saved. No tools were executed.')
            raise
        except Exception:
            await asyncio.to_thread(self.planning_failed, task_id, owner, 'Planner returned an invalid execution plan. No tools were executed.')
            raise ExecutionError('invalid_execution_plan', 'Planner returned an invalid execution plan. No tools were executed.', 502) from None
        return await asyncio.to_thread(response, self.path, task_id)

    def approve(self, task_id: str, body: ApprovalRequest) -> str | None:
        if not self.settings.execution_enabled:
            raise ExecutionError('execution_disabled', 'Workspace execution is disabled.', 503)
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            run = get_run(db, task_id)
            task = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if (run['phase'] != 'awaiting_approval' or task['status'] != 'awaiting_approval'
                    or body.plan_digest != run['plan_digest']):
                raise ExecutionError('approval_conflict', 'Approval must match the current pending execution plan.', 409)
            plan = load_plan(db, run, self.workspace.identity)
            if not self.safety.assess(plan) or not run['approval_required']:
                raise ExecutionError('approval_conflict', 'The saved approval requirement is inconsistent.', 409)
            db.execute('INSERT INTO execution_approvals VALUES (?,?,?,?)', (task_id, body.plan_digest, body.decision, timestamp()))
            if body.decision == 'reject':
                transition(db, task_id, 'cancelled', '[Safety] Human rejected execution. No tools were executed.')
                return None
            owner = str(uuid4())
            db.execute('UPDATE execution_runs SET owner=? WHERE task_id=?', (owner, task_id))
            transition(db, task_id, 'executing', '[Safety] Human approved the exact saved plan. [Executor] Execution started.', lease=self.settings.execution_lease_seconds)
            return owner

    def authorize(self, db, task_id: str, owner: str):
        if not self.settings.execution_enabled:
            raise ExecutionError('execution_disabled', 'Workspace execution is disabled.', 503)
        run = require_owner(db, task_id, owner)
        task = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not run['approval_required'] or task['status'] != 'running':
            raise ExecutionError('approval_conflict', 'Execution task state or approval requirement is inconsistent.', 409)
        plan = load_plan(db, run, self.workspace.identity)
        self.safety.assess(plan)
        approval = db.execute("SELECT 1 FROM execution_approvals WHERE task_id=? AND plan_digest=? AND decision='approve'",
                              (task_id, run['plan_digest'])).fetchone()
        if not approval:
            raise ExecutionError('approval_required', 'Explicit approval is required before any tool execution.', 409)
        return plan

    def fail(self, task_id: str, owner: str, message: str):
        try:
            with connection(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                run = get_run(db, task_id)
                if run['owner'] == owner and run['phase'] in {'executing', 'verifying'}:
                    transition(db, task_id, 'failed', '[Orchestrator] ' + message, error=message)
                    db.execute("UPDATE execution_steps SET status='failed',error=? WHERE task_id=? AND status='executing'", (message, task_id))
                    db.execute('UPDATE tasks SET final_result=? WHERE id=?', (self.result_summary(db, task_id), task_id))
        except sqlite3.Error:
            logger.error('Execution failure could not be saved; lease recovery will retry.')

    def execute(self, task_id: str, owner: str):
        try:
            with connection(self.path) as db:
                plan = self.authorize(db, task_id, owner)
            if plan.workflow == 'python':
                from app.execution.python_workflow import execute_python
                return execute_python(self, task_id, owner, plan)
            for step in plan.steps:
                role = 'Executor'
                for attempt in range(self.settings.execution_max_attempts):
                    # Short transactions fence recovery and other workers while bounded local I/O runs.
                    # No model/network calls are made under a database write lock.
                    with connection(self.path) as db:
                        db.execute('BEGIN IMMEDIATE')
                        self.authorize(db, task_id, owner)
                        current = db.execute('SELECT * FROM execution_steps WHERE task_id=? AND number=?', (task_id, step.number)).fetchone()
                        if current['status'] != 'pending':
                            raise ExecutionError('step_conflict', 'Step ownership changed. Reload execution history.', 409)
                        for dependency in step.dependencies:
                            row = db.execute('SELECT status FROM execution_steps WHERE task_id=? AND number=?', (task_id, dependency)).fetchone()
                            if row['status'] != 'executed':
                                raise ExecutionError('dependency_failed', 'A required step has not completed execution.', 409)
                        db.execute("UPDATE execution_steps SET status='executing',attempts=attempts+1 WHERE task_id=? AND number=?", (task_id, step.number))
                        history(db, task_id, 'running', f'[{role}] Step {step.number}: attempt {attempt + 1} started; inputs: {step.dependencies}.', timestamp())
                    retry = False
                    with connection(self.path) as db:
                        db.execute('BEGIN IMMEDIATE')
                        self.authorize(db, task_id, owner)
                        try:
                            prior = {n: ToolResult.model_validate_json(db.execute(
                                'SELECT result FROM execution_steps WHERE task_id=? AND number=?',
                                (task_id, n)).fetchone()['result']) for n in step.dependencies}
                            if plan.workflow == 'five_agent':
                                actual = self.executor.execute_step(plan, step, prior, self.verifier)
                            else:
                                actual = self.executor.execute(step.tool)
                            result = ToolResult.model_validate(actual.model_dump())
                            check_secrets(self.settings, result.model_dump_json())
                            db.execute("UPDATE execution_steps SET status='executed',error=NULL,result=? WHERE task_id=? AND number=?", (result.model_dump_json(), task_id, step.number))
                            history(db, task_id, 'running', f'[{role}] Step {step.number}: actual output recorded; verification pending.', timestamp())
                        except ToolFailure as exc:
                            # Only explicitly transient, read-only failures can be replayed.
                            retry = exc.retryable and step.tool.name != 'write_text' and attempt + 1 < self.settings.execution_max_attempts
                            safe = 'Workspace tool failed. Check the file, format and permissions; no successful result was recorded.'
                            db.execute('UPDATE execution_steps SET status=?,error=? WHERE task_id=? AND number=?',
                                       ('pending' if retry else 'failed', safe, task_id, step.number))
                            history(db, task_id, 'running', f'[{role}] Step {step.number}: ' + ('bounded retry scheduled.' if retry else 'failed.'), timestamp())
                            if not retry:
                                transition(db, task_id, 'failed', f'[{role}] ' + safe, error=safe)
                                db.execute('UPDATE tasks SET final_result=? WHERE id=?', (self.result_summary(db, task_id), task_id))
                                return
                        db.execute('UPDATE execution_runs SET expires_at=?,updated_at=? WHERE task_id=?', (expiry(self.settings.execution_lease_seconds), timestamp(), task_id))
                    if not retry: break
            with connection(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                self.authorize(db, task_id, owner)
                transition(db, task_id, 'verifying', '[Verifier] Independently checking actual files and rerunning JSON checks.', lease=self.settings.execution_lease_seconds)
            # All final checks share a short transaction; no other AgentX run can mutate
            # workspace files between these checks and the completed commit.
            with connection(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                self.authorize(db, task_id, owner)
                reviewed = None
                if plan.workflow == 'five_agent':
                    results = {r['number']: ToolResult.model_validate_json(r['result']) for r in db.execute(
                        'SELECT number,result FROM execution_steps WHERE task_id=?', (task_id,))}
                    try:
                        reviewed = self.verifier.review(self.safety, plan, results)
                    except (ToolFailure, ExecutionError):
                        reviewed = {s.number: Verification(passed=False, checks=['review_evidence_unavailable_or_invalid']) for s in plan.steps}
                for step in plan.steps:
                    row = db.execute('SELECT * FROM execution_steps WHERE task_id=? AND number=?', (task_id, step.number)).fetchone()
                    if row['status'] != 'executed':
                        raise ExecutionError('missing_output', 'A required step has no recorded output.', 409)
                    result = ToolResult.model_validate_json(row['result'])
                    try: verification = Verification.model_validate((reviewed[step.number] if reviewed is not None else self.verifier.verify(step.tool, result)).model_dump())
                    except (ToolFailure, ExecutionError): verification = Verification(passed=False, checks=['actual_output_unavailable_or_invalid'])
                    check_secrets(self.settings, verification.model_dump_json())
                    db.execute('UPDATE execution_steps SET status=?,verification=?,error=? WHERE task_id=? AND number=?',
                        ('verified' if verification.passed else 'failed', verification.model_dump_json(), None if verification.passed else 'Actual output verification failed.', task_id, step.number))
                    history(db, task_id, 'running', f'[Verifier] Step {step.number}: ' + ('actual output verified.' if verification.passed else 'verification failed.'), timestamp())
                    if not verification.passed:
                        transition(db, task_id, 'failed', '[Verifier] Actual output verification failed. Task was not completed.', error='Actual output verification failed.')
                        db.execute('UPDATE tasks SET final_result=? WHERE id=?', (self.result_summary(db, task_id), task_id))
                        return
                summary = self.result_summary(db, task_id)
                db.execute('UPDATE tasks SET final_result=? WHERE id=?', (summary, task_id))
                transition(db, task_id, 'completed', '[Orchestrator] ' + summary)
        except sqlite3.Error:
            self.fail(task_id, owner, 'Execution storage was unavailable. Outputs may be partial; inspect the workspace before starting another task.')
            raise
        except Exception:
            self.fail(task_id, owner, 'Execution stopped safely. Outputs may be partial; inspect the workspace before starting another task.')
            # Unexpected tool/model/provider details never enter history or public errors.
            raise ExecutionError('execution_failed', 'Execution stopped safely. Reload task and execution history.', 502) from None

    @staticmethod
    def result_summary(db, task_id):
        rows = db.execute('SELECT * FROM execution_steps WHERE task_id=? ORDER BY number', (task_id,)).fetchall()
        lines = ['Completed tasks and file evidence:']
        tests = []
        remaining = []
        for row in rows:
            call = json.loads(row['tool_json'])
            result = json.loads(row['result']) if row['result'] else None
            label = 'created' if call['name'] == 'write_text' else 'checked'
            lines.append(f"- Step {row['number']}: {row['status']} — {call['name']} {call.get('path', '')}")
            if result:
                lines.append(f"  File {label}: {result['path']}; SHA-256 {result['sha256']}; {result['byte_count']} bytes.")
            if call['name'] == 'validate_json':
                tests.append(f"- validate_json({call['path']}, required_keys={call['required_keys']}): " + ('PASSED' if row['status'] == 'verified' else 'FAILED' if row['status'] == 'failed' else 'NOT TESTED'))
            if row['status'] != 'verified': remaining.append(str(row['number']))
        lines += ['Actual test results:', *(tests or ['- NOT TESTED: no test tool was in the approved plan.']),
                  '- Application behavior, integration and browser tests: NOT TESTED (no approved runner).',
                  'Failed or blocked operations: ' + ('; '.join(r['error'] for r in rows if r['error']) or 'None recorded.'),
                  'Remaining work: ' + ('steps ' + ', '.join(remaining) if remaining else 'None within the approved file plan; broader functionality is not verified.'),
                  'Verified final deliverable: ' + ('approved file outputs only.' if not remaining else 'NOT VERIFIED; outputs may be partial.')]
        return '\n'.join(lines)
