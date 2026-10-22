import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import AgentProgress from './AgentProgress';
import { isExecutionProgress } from './api/client';

const id = '00000000-0000-4000-8000-000000000001';
const progress = (phase = 'awaiting_approval') => ({ task_id: id, phase, plan_digest: 'a'.repeat(64),
  agents: ['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier'].map((role, i) => ({ role,
    status: phase === 'completed' || i === 1 ? 'succeeded' : 'pending', attempts: i === 1 || phase === 'completed' ? 1 : 0,
    input_summary: 'Saved prior evidence', output_summary: phase === 'completed' ? 'Actual output verified' : 'Waiting for approval',
    input_steps: i > 1 ? [i - 1] : [], evidence: [], checks: [], error: null })) });
const reply = (data: unknown) => ({ ok: true, json: async () => data });

describe('Agent evidence', () => {
  it('shows persisted pending roles without inventing execution or submitting approval', async () => {
    const fetch = vi.fn().mockResolvedValue(reply(progress()));
    vi.stubGlobal('fetch', fetch);
    render(<AgentProgress taskId={id} />);
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Show agent activity' }));
    expect(await screen.findByText('Executor Agent — pending')).toBeInTheDocument();
    expect(screen.getByText('Planner Agent — succeeded')).toBeInTheDocument();
    for (const role of ['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier']) {
      expect(screen.getByRole('region', { name: `${role} Agent` })).toBeInTheDocument();
    }
    expect(screen.queryByRole('region', { name: 'Research Agent' })).not.toBeInTheDocument();
    expect(screen.getByText(/No tools have run/)).toBeInTheDocument();
    expect(screen.getByText('Workflow phase: awaiting_approval')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument();
    expect(fetch.mock.calls.every(([, options]) => !options.method)).toBe(true);
  });
  it('refreshes from saved status to completed with tool evidence', async () => {
    const done = progress('completed');
    Object.assign(done.agents[3], { evidence: [{ path: 'source.json', sha256: 'b'.repeat(64), byte_count: 13, content: '{"answer":42}', checks: ['valid_json'] }] });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(reply(progress('executing'))).mockResolvedValue(reply(done)));
    render(<AgentProgress taskId={id} />);
    fireEvent.click(screen.getByRole('button', { name: 'Show agent activity' }));
    await waitFor(() => expect(screen.getByText('Verifier Agent — succeeded')).toBeInTheDocument(), { timeout: 3000 });
    expect(screen.getByText('Tool evidence: source.json (13 bytes)')).toBeInTheDocument();
    expect(screen.getByText('{"answer":42}')).toBeInTheDocument();
  });
  it('renders test failures and skipped review honestly', async () => {
    const failed = progress('failed');
    Object.assign(failed.agents[3], { status: 'failed', attempts: 2, error: 'JSON checks failed.' });
    Object.assign(failed.agents[4], { status: 'skipped' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(failed)));
    render(<AgentProgress taskId={id} />);
    fireEvent.click(screen.getByRole('button', { name: 'Show agent activity' }));
    expect(await screen.findByText('Executor Agent — failed')).toBeInTheDocument();
    expect(screen.getByText('Verifier Agent — skipped')).toBeInTheDocument();
    expect(screen.getByText('JSON checks failed.')).toBeInTheDocument();
  });
  it('rejects malformed evidence and offers a read-only retry', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(reply({ ...progress(), agents: [{ role: 'Executor', status: 'succeeded' }] })).mockResolvedValue(reply(progress())));
    render(<AgentProgress taskId={id} />);
    fireEvent.click(screen.getByRole('button', { name: 'Show agent activity' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('invalid task response');
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading activity' }));
    expect(await screen.findByText('Verifier Agent — pending')).toBeInTheDocument();
    expect(isExecutionProgress({ ...progress(), phase: 'fake_success' })).toBe(false);
    expect(isExecutionProgress({ ...progress(), agents: progress().agents.slice(1) })).toBe(false);
  });
  it('reloads an open panel when the saved task status changes after planning', async () => {
    const pending = progress('pending');
    Object.assign(pending.agents[1], {status: 'pending'});
    const fetch = vi.fn().mockResolvedValueOnce(reply(pending)).mockResolvedValue(reply(progress()));
    vi.stubGlobal('fetch', fetch);
    const page = render(<AgentProgress taskId={id} status="pending" />);
    fireEvent.click(screen.getByRole('button', {name:'Show agent activity'}));
    await screen.findByText('Planner Agent — pending');
    page.rerender(<AgentProgress taskId={id} status="awaiting_approval" />);
    expect(await screen.findByText('Planner Agent — succeeded')).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('shows explicit static-check statuses without claiming execution or correctness', async () => {
    const saved = progress('failed');
    const report = {code_executed:false, factual_correctness_verified:false, answer_sha256:'a'.repeat(64), checks:[
      {name:'answer_hash',category:'integrity',status:'passed',detail:'Computed from the answer.'},
      {name:'requested_function',category:'static_code',status:'failed',detail:'Requested definition absent.'},
      {name:'semantic_correctness',category:'correctness',status:'not_checked',detail:'No behavior checks ran.'},
    ]};
    Object.assign(saved.agents[4], {status:'failed',text_verification:report});
    expect(isExecutionProgress(saved)).toBe(true);
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(reply(saved)));
    render(<AgentProgress taskId={id}/>);
    fireEvent.click(screen.getByRole('button',{name:'Show agent activity'}));
    expect(await screen.findByRole('region',{name:'Text verification report'})).toHaveTextContent('code_executed: false');
    expect(screen.getByText('factual_correctness_verified: false')).toBeInTheDocument();
    expect(screen.getByText(/static_code \/ requested_function: failed/)).toBeInTheDocument();
    expect(screen.getByText(/correctness \/ semantic_correctness: not_checked/)).toBeInTheDocument();
    Object.assign(saved.agents[4],{text_verification:{...report,code_executed:true}});
    expect(isExecutionProgress(saved)).toBe(false);
    Object.assign(saved.agents[4],{text_verification:{...report,factual_correctness_verified:true}});
    expect(isExecutionProgress(saved)).toBe(false);
    Object.assign(saved.agents[4],{text_verification:{...report,checks:[{...report.checks[0],status:'verified'}]}});
    expect(isExecutionProgress(saved)).toBe(false);
  });

});
