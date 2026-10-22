"""Five agent states reconstructed from durable lifecycle and tool evidence."""
from app.execution.models import AgentState, StepState, ToolResult
from app.execution.python_models import PythonEvidence


def agent_states(run, steps: list[StepState], review_started: bool, decision=None) -> list[AgentState]:
    phase = run['phase']
    stopped = phase in {'failed', 'cancelled'}
    planned = bool(run['plan_json'])
    evidence = [s.result for s in steps if isinstance(s.result, ToolResult)]
    sandbox = [s.result for s in steps if isinstance(s.result, PythonEvidence)]
    sandbox_failed = any(e.error or not e.cleanup_confirmed or any(
        c.exit_code != 0 or c.timed_out or c.oom or c.overflow or not c.stopped or not c.removed
        for c in e.cases) for e in sandbox)
    checks = [c for s in steps if s.verification for c in s.verification.checks]
    verified = phase == 'completed' and bool(steps) and all(s.verification and s.verification.passed for s in steps)
    executed = bool(steps) and all(s.result for s in steps)
    attempted = any(s.attempts for s in steps)
    def state(role, status, inputs, output, attempts=0, results=None, checked=None, error=None):
        return AgentState(role=role, status=status, attempts=attempts, input_summary=inputs,
            output_summary=output, input_steps=[s.number for s in steps] if role in {'Executor', 'Verifier'} else [],
            evidence=results or [], sandbox_evidence=sandbox if role in {'Executor', 'Verifier'} else [],
            checks=checked or [], error=error)
    return [
        state('Orchestrator', 'succeeded' if verified else 'failed' if stopped else 'running',
              'User goal, lifecycle, saved approval and role results.',
              'Combined verified result saved.' if verified else 'Coordinating phase: ' + phase, 1, error=run['error'] if stopped else None),
        state('Planner', 'succeeded' if planned else 'failed' if stopped else 'running',
              'User task and structured planning schema.',
              ('Clarification required; no executable plan approved.' if phase == 'awaiting_user_input' else 'Validated plan saved; digest: ' + (run['plan_digest'] or 'none')) if planned else 'No validated plan saved.', 1,
              error=run['error'] if stopped and not planned else None),
        state('Safety', 'succeeded' if decision == 'approve' else 'failed' if stopped and planned else 'running' if planned else 'skipped' if stopped else 'pending',
              'Policy, workspace restrictions and exact saved plan digest.',
              'Explicit approval recorded; per-tool policy checks enforced.' if decision == 'approve' else
              'Policy validated; waiting for explicit digest-bound approval. No tools authorized.' if planned and not stopped else 'No tool authorization granted.',
              1 if planned else 0, error=run['error'] if stopped and planned and decision != 'approve' else None),
        state('Executor', 'failed' if sandbox_failed or any(s.status == 'failed' and not s.verification for s in steps) else 'succeeded' if executed else 'skipped' if stopped else 'running' if attempted else 'pending',
              'Approved tool calls and dependency evidence; local research, implementation and JSON tests.',
              f'{len(evidence) + len(sandbox)} actual tool results saved.' if evidence or sandbox else 'No successful tool output recorded.',
              max((s.attempts for s in steps), default=0), evidence,
              [c for s in steps if s.result for c in s.result.checks],
              run['error'] if stopped and attempted and not executed else None),
        state('Verifier', 'succeeded' if verified else 'failed' if stopped and review_started else 'skipped' if stopped else 'running' if review_started else 'pending',
              'Saved requirements, actual outputs, hashes, content and test evidence.',
              'Independent output, JSON and policy checks passed.' if verified else 'No passing final verification.',
              1 if review_started else 0, evidence if review_started else [], checks,
              run['error'] if stopped and review_started else None),
    ]
