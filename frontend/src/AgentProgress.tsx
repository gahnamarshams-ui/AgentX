import { useEffect, useState } from 'react';
import { getExecutionProgress } from './api/client';
import type { ExecutionProgress } from './api/client';

const titles = { Orchestrator: 'Team Leader', Planner: 'Thinker', Safety: 'Security Guard', Executor: 'Doer', Verifier: 'Quality Checker' };

export default function AgentProgress({ taskId, status }: { taskId: string; status?: string }) {
  const [open, setOpen] = useState(false);
  const [retry, setRetry] = useState(0);
  const [data, setData] = useState<ExecutionProgress | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    setData(null); setError('');
    if (!open) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      try {
        const saved = await getExecutionProgress(taskId, controller.signal);
        if (!active) return;
        if (saved.task_id !== taskId) throw new Error('Backend returned progress for a different task.');
        setData(saved); setError('');
        if (['planning', 'executing', 'verifying'].includes(saved.phase)) timer = setTimeout(poll, 1500);
      } catch (e) { if (active) setError(e instanceof Error ? e.message : 'Could not load agent evidence.'); }
    }
    void poll();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [open, retry, taskId, status]);
  return <section aria-label="Agent execution evidence">
    <button className="text-action" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? 'Hide agent activity' : 'Show agent activity'}</button>
    {open && <>
      {!data && !error && <p role="status">Loading saved agent evidence…</p>}
      {error && <><p role="alert" className="error">{error}</p><button className="text-action" onClick={() => setRetry(retry + 1)}>Retry loading activity</button></>}
      {data && <>
        <p role="status">Workflow phase: {data.phase}</p>
        {data.phase === 'awaiting_approval' && <p>{data.plan_digest ? 'No tools have run. Approval of the exact saved execution plan is required through the API.' : 'No tools have run. This is a proposal-only plan; there is no executable plan or approval digest.'}</p>}
        {data.plan_digest && <details><summary>Saved plan digest</summary><code style={{ overflowWrap: 'anywhere' }}>{data.plan_digest}</code></details>}
        {data.agents.map(agent => <section key={agent.role} aria-label={`${agent.role} Agent`}>
          <h3>{agent.role} Agent — {agent.status}</h3>
          <p>{titles[agent.role]}</p><p>Attempts: {agent.attempts}</p>
          <p>Input: {agent.input_summary}</p><p>Output: {agent.output_summary}</p>
          {!!agent.input_steps.length && <p>Uses step evidence: {agent.input_steps.join(', ')}</p>}
          {agent.error && <p className="error">{agent.error}</p>}
          {!!agent.checks.length && <ul>{agent.checks.map((check, i) => <li key={i}>{check}</li>)}</ul>}
          {agent.text_verification && <section aria-label="Text verification report">
            <p>code_executed: {String(agent.text_verification.code_executed)}</p>
            <p>factual_correctness_verified: {String(agent.text_verification.factual_correctness_verified)}</p>
            <p>Static checks do not establish code behavior or factual correctness.</p>
            <ul>{agent.text_verification.checks.map(check => <li key={check.name}>
              {check.category} / {check.name}: {check.status} — {check.detail}
            </li>)}</ul>
          </section>}
          {agent.evidence.map((e, i) => <details key={i}><summary>Tool evidence: {e.path} ({e.byte_count} bytes)</summary>
            <p style={{ overflowWrap: 'anywhere' }}>SHA-256: {e.sha256}</p>
            {e.content !== null && <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{e.content}</pre>}
            <p>{e.checks.join(', ')}</p>
          </details>)}
        </section>)}
      </>}
    </>}
  </section>;
}
