import { useEffect, useRef, useState } from 'react';
import { approveWorkspace, clarifyWorkspace, getWorkspaceRun } from './api/client';
import type { Task, WorkspaceRun } from './api/client';

export default function ExecutionPanel({task, onReload}: {task: Task; onReload: () => void}) {
  const [run, setRun] = useState<WorkspaceRun | null>(null);
  const [error, setError] = useState('');
  const [answer, setAnswer] = useState('');
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  useEffect(() => {
    let active = true;
    getWorkspaceRun(task.id).then(value => {
      if (value.task_id !== task.id) throw new Error('Execution belongs to another task.');
      if (active) {setRun(value); setError('');}
    }).catch(e => {if (active) setError(e.message);});
    return () => {active = false;};
  }, [task.id, task.status]);
  async function act(decision?: 'approve' | 'reject') {
    if (lock.current || !run) return;
    lock.current = true; setBusy(true); setError('');
    try {
      const saved = decision && run.plan_digest
        ? await approveWorkspace(task.id, run.plan_digest, decision)
        : await clarifyWorkspace(task.id, answer.trim());
      if (saved.task_id !== task.id) throw new Error('Execution belongs to another task.');
      setRun(saved); setAnswer('');
    } catch(e) {setError(e instanceof Error ? e.message : 'Operation failed. Reload saved state.');}
    finally {lock.current = false; setBusy(false); onReload();}
  }
  return <section aria-label="Workspace approval and evidence">
    <p>Only the exact saved plan below can be approved. No overwrites, arbitrary code, shell or deployment. Application behavior: NOT TESTED.</p>
    {error && <p role="alert">{error}</p>}
    {run && <>
      <p>Saved execution phase: {run.phase}</p>
      {run.steps.map(step => <details key={step.number}><summary>Step {step.number}: {step.tool.name} {step.tool.path} — {step.status}</summary>
        <pre>{JSON.stringify(step.tool, null, 2)}</pre>
        {step.tool.content != null && <><p>Exact file content:</p><pre>{step.tool.content}</pre></>}
        {step.result && <p>Actual SHA-256: {step.result.sha256}</p>}
        <p>Verification: {step.verification ? step.verification.passed ? 'PASSED' : 'FAILED' : 'NOT TESTED'}</p>
        {step.verification && <p>{step.verification.checks.join(', ')}</p>}
        {step.error && <p>{step.error}</p>}
      </details>)}
      {run.phase === 'awaiting_approval' && run.plan_digest && <>
        <p>Approval digest: <code>{run.plan_digest}</code></p>
        <button disabled={busy} onClick={() => void act('approve')}>Approve exact workspace actions</button>
        <button disabled={busy} onClick={() => void act('reject')}>Reject workspace actions</button>
      </>}
      {run.phase === 'awaiting_user_input' && <form onSubmit={e => {e.preventDefault(); void act();}}>
        <label>Clarification for the original task<textarea required maxLength={5000} value={answer} onChange={e => setAnswer(e.target.value)} /></label>
        <button disabled={busy || !answer.trim()}>Continue original task</button>
      </form>}
      {run.error && <p role="alert">{run.error}</p>}
    </>}
  </section>;
}
