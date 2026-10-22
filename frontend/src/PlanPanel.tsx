import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { generateAnswer, getTask, orchestrate } from './api/client';
import type { Task } from './api/client';
import { AgentMark } from './Icon';
import Icon from './Icon';
import AgentProgress from './AgentProgress';
import Markdown from './Markdown';
const ExecutionPanel = lazy(() => import('./ExecutionPanel'));
const PlanResponse = lazy(() => import('./PlanResponse'));

export default function PlanPanel({ task, onReload, onStatusChange, autoPlan = false, onAutoPlanStarted, workspaceRequested = false }: { task: Task; onReload: () => void; onStatusChange?: (task: Task) => void; autoPlan?: boolean; workspaceRequested?: boolean; onAutoPlanStarted?: () => void }) {
  const [current, setCurrent] = useState(task);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const statusCallback = useRef(onStatusChange);
  statusCallback.current = onStatusChange;
  function updateSaved(saved: Task) { setCurrent(saved); statusCallback.current?.(saved); }
  const lock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { setCurrent(task); }, [task]);

  // Poll actual saved state. No invented percentage, step completion, or elapsed progress.
  useEffect(() => {
    if (!waiting && !['planning', 'running'].includes(current.status)) return;
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const saved = await getTask(task.id, controller.signal);
        if (active) {
          updateSaved(saved); setPollError('');
          if (current.status === 'running' && saved.status !== 'running') onReload();
        }
      } catch {
        if (active) setPollError('Could not refresh task status. Use Reload details to check the saved state.');
      } finally {
        if (active) timer = setTimeout(poll, 2000);
      }
    }
    void poll();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [waiting, current.status, task.id, onReload]);

  async function plan() {
    if (lock.current) return;
    lock.current = true; setWaiting(true); setError('');
    try {
      if (workspaceRequested) await orchestrate(task.id);
      const saved = workspaceRequested ? await getTask(task.id) : await generateAnswer(task.id);
      if (mounted.current) updateSaved(saved);
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : 'Planning failed. Reload details and retry.');
    } finally {
      lock.current = false;
      if (mounted.current) {
        setWaiting(false);
        // Refresh task and chronological history without clearing the operation's error.
        try {
          const saved = await getTask(task.id);
          if (mounted.current) { updateSaved(saved); onReload(); }
        } catch { if (mounted.current) setPollError('Could not reload the saved task. Please reload details.'); }
      }
    }
  }
  const autoRequested = useRef(false);
  const autoStart = useRef({plan, onAutoPlanStarted});
  autoStart.current = {plan, onAutoPlanStarted};
  useEffect(() => {
    if (!autoPlan || autoRequested.current) return;
    autoRequested.current = true;
    autoStart.current.onAutoPlanStarted?.();
    if (task.status === 'pending' && !task.plan) void autoStart.current.plan();
  }, [autoPlan, task.id, task.status, task.plan]);
  const eligible = !current.plan && ['pending', 'failed'].includes(current.status);
  const planData = current.plan;
  return <article className="assistant-message" aria-label="AgentX planning response">
    <div className="assistant-avatar"><AgentMark /></div>
    <div className="assistant-content">
      <div className="message-byline"><strong>AgentX</strong><span>Assistant</span></div>
      {!planData && !['completed', 'awaiting_user_input', 'blocked'].includes(current.status) && <div className="planning-intro"><h2>{waiting || current.status === 'planning' ? 'Thinking…' : current.status === 'failed' ? 'Response needs another try' : 'Ready to help.'}</h2>
        <p className="muted">Send a message to get an answer. Code appears as copyable text; running tools requires separate approval.</p></div>}
      {eligible && <button className="primary-action" disabled={waiting} onClick={() => void plan()}>
        {waiting ? <span className="spinner" /> : <Icon name="spark" />}
        {waiting ? 'Writing response…' : current.status === 'failed' ? 'Retry response' : 'Get answer'}
      </button>}
      {waiting && current.status !== 'planning' && <p role="status" className="planning-state">Preparing your answer…</p>}
      {current.status === 'planning' && <p role="status" className="planning-state"><span className="spinner" />Thinking and preparing your answer…</p>}
      {current.status === 'running' && <p role="status" className="planning-state"><span className="spinner" />Backend status: running. Execution and verification are in progress.</p>}
      {current.status === 'blocked' && <p role="status">Blocked — implementation has not completed.</p>}
      {['completed', 'awaiting_user_input', 'failed', 'blocked'].includes(current.status) && current.final_result && (current.response_mode === 'chat' ? <section aria-label="Assistant answer"><Markdown>{current.final_result}</Markdown></section> : <section aria-label={current.status === 'completed' ? 'Verified task result' : 'Task state and evidence'}><h2>{current.status === 'awaiting_user_input' ? 'Clarification required' : current.status === 'failed' || current.status === 'blocked' ? 'Execution stopped' : 'Verified result'}</h2><Markdown>{current.final_result}</Markdown></section>)}
      {error && <p role="alert" className="error">{error}</p>}
      {pollError && <p role="alert" className="error">{pollError}</p>}
      {current.error && current.error !== error && <p className="error">{current.error}</p>}
      {planData && current.response_mode !== 'chat' && <details className="internal-plan"><summary>Earlier saved plan</summary><Suspense fallback={<p role="status" className="muted">Formatting saved plan…</p>}><PlanResponse plan={planData} status={current.status} /></Suspense></details>}
      {!waiting && <button className="text-action" onClick={onReload}><Icon name="refresh" />Reload answer and history</button>}
      {current.response_mode !== 'chat' && planData && <Suspense fallback={<p>Loading workspace controls...</p>}><ExecutionPanel task={current} onReload={onReload} /></Suspense>}
      <AgentProgress taskId={task.id} status={current.status} />
      {!planData && <p className="privacy-note">Your message is sent to OpenAI to generate an answer. Code is not executed and workspace files are not changed.</p>}
    </div>
  </article>;
}
