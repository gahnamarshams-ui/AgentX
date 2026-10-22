import { StrictMode } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import PlanPanel from './PlanPanel';
import { getTask, isPlan } from './api/client';
import type { Task, TaskPlan } from './api/client';
const plan: TaskPlan = { goal: 'Prepare a schedule', assumptions: [], constraints: ['One hour daily'], clarification_needed: true,
  clarification_questions: ['When is the exam?'], steps: [
    { number: 1, action: 'Confirm exam date', purpose: 'Bound schedule', expected_output: 'Date', dependencies: [] },
    { number: 2, action: 'Draft schedule', purpose: 'Allocate time', expected_output: 'Schedule', dependencies: [1] },
  ] };
const task: Task = { id: '00000000-0000-4000-8000-000000000001', description: 'Study', status: 'pending', created_at: '2026-10-19T12:00:00Z', updated_at: '2026-10-19T12:00:00Z', plan: null, final_result: null, error: null };
const reply = (data: unknown) => ({ ok: true, json: async () => data });

describe('AI planning', () => {
  it('renders saved plan, clarification, expected outputs and dependencies as text', async () => {
    render(<PlanPanel task={{ ...task, status: 'awaiting_approval', plan }} onReload={vi.fn()} />);
    expect(await screen.findByText('Prepare a schedule')).toBeInTheDocument();
    expect(screen.getByText('When is the exam?')).toBeInTheDocument();
    expect(screen.getByText('Dependencies: Step 1')).toBeInTheDocument();
    expect(screen.getByText('Plan saved for review. No steps have been executed.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Get answer' })).not.toBeInTheDocument();
  });
  it('uses actual backend planning state and reloads history after success', async () => {
    let resolvePlan!: (value: unknown) => void;
    let current = task;
    const reload = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockImplementation((_url: string, options?: RequestInit) => {
      if (options?.method === 'POST') { current = { ...task, status: 'planning' }; return new Promise(resolve => { resolvePlan = resolve; }); }
      return Promise.resolve(reply(current));
    }));
    render(<PlanPanel task={task} onReload={reload} />);
    fireEvent.click(screen.getByRole('button', { name: 'Get answer' }));
    expect(await screen.findByText('Thinking and preparing your answer…')).toBeInTheDocument();
    current = { ...task, status: 'awaiting_approval', plan };
    resolvePlan(reply(current));
    expect(await screen.findByText('Prepare a schedule')).toBeInTheDocument();
    await waitFor(() => expect(reload).toHaveBeenCalledOnce());
  });
  it('reports provider failure and allows an explicit retry', async () => {
    let current = task;
    let calls = 0;
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (_url: string, options?: RequestInit) => {
      if (options?.method === 'POST') {
        if (calls++ === 0) { current = { ...task, status: 'failed', error: 'AI planning timed out.' }; return { ok: false, status: 504, json: async () => ({ error: { message: 'AI planning timed out.' } }) }; }
        current = { ...task, status: 'awaiting_approval', plan };
      }
      return reply(current);
    }));
    render(<PlanPanel task={task} onReload={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Get answer' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('AI planning timed out.');
    fireEvent.click(await screen.findByRole('button', { name: 'Retry response' }));
    expect(await screen.findByText('Plan saved for review. No steps have been executed.')).toBeInTheDocument();
    expect(calls).toBe(2);
  });
  it('rejects malformed and cyclic plans from the backend', async () => {
    const bad = { ...plan, steps: [{ ...plan.steps[0], dependencies: [1] }] };
    expect(isPlan(bad)).toBe(false);
    expect(isPlan({ ...plan, clarification_needed: false })).toBe(false);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply({ ...task, plan: bad })));
    await expect(getTask(task.id)).rejects.toThrow('invalid task response');
  });
  it('leaves missing-key failure visible and keeps pending task retryable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (_url: string, options?: RequestInit) => options?.method === 'POST'
      ? { ok: false, status: 503, json: async () => ({ error: { message: 'Set OPENAI_API_KEY on the backend.' } }) } : reply(task)));
    render(<PlanPanel task={task} onReload={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Get answer' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Set OPENAI_API_KEY on the backend.');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Get answer' })).not.toBeDisabled());
  });
});

describe('Persisted execution status', () => {
  it('polls a running task, displays verified result and reloads activity', async () => {
    const saved = { ...task, plan, status: 'completed', final_result: 'Verified 2 workspace steps.' };
    const reload = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(saved)));
    render(<PlanPanel task={{ ...task, plan, status: 'running' }} onReload={reload} />);
    expect(screen.getByText('Backend status: running. Execution and verification are in progress.')).toBeInTheDocument();
    expect(await screen.findByText('Verified 2 workspace steps.')).toBeInTheDocument();
    expect(await screen.findByText('The backend reports verified completion. See the saved result and task activity.')).toBeInTheDocument();
    expect(screen.queryByText('Plan saved for review. No steps have been executed.')).not.toBeInTheDocument();
    await waitFor(() => expect(reload).toHaveBeenCalledOnce());
    expect(screen.queryByRole('button', { name: 'Get answer' })).not.toBeInTheDocument();
  });
  it('reports execution polling failure without inventing completion', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    render(<PlanPanel task={{ ...task, plan, status: 'running' }} onReload={vi.fn()} />);
    expect(await screen.findByText(/Could not refresh task status/)).toHaveTextContent('Could not refresh task status.');
    expect(screen.queryByText('Verified result')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reload answer and history' })).toBeEnabled();
  });
});


describe('automatic planning after send', () => {
  it('requests planning once under StrictMode and displays the saved response', async () => {
    const saved: Task = {...task, status:'awaiting_approval', plan};
    const fetch = vi.fn().mockResolvedValue(reply(saved));
    vi.stubGlobal('fetch', fetch);
    render(<StrictMode><PlanPanel task={task} autoPlan onAutoPlanStarted={vi.fn()} onReload={vi.fn()} /></StrictMode>);
    expect(await screen.findByText('Prepare a schedule')).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([,o]) => o?.method === 'POST')).toHaveLength(1);
    expect(fetch.mock.calls.find(([,o]) => o?.method === 'POST')?.[0]).toMatch(/\/respond$/);
    expect(fetch.mock.calls.some(([url])=>/orchestrate|approval/.test(url))).toBe(false);
  });
  it('does not automatically retry failed planning or create another task', async () => {
    const failed: Task = {...task, status:'failed', error:'AI service unavailable.'};
    const fetch = vi.fn().mockImplementation(async (_url, options) => options?.method === 'POST'
      ? {ok:false,status:503,json:async()=>({error:{message:'AI service unavailable.'}})} : reply(failed));
    vi.stubGlobal('fetch', fetch);
    render(<PlanPanel task={task} autoPlan onReload={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('AI service unavailable.');
    expect(await screen.findByRole('button',{name:'Retry response'})).toBeEnabled();
    expect(fetch.mock.calls.filter(([,o])=>o?.method === 'POST')).toHaveLength(1);
  });
  it('opening an existing pending task does not trigger planning', () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    render(<PlanPanel task={task} onReload={vi.fn()} />);
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.getByRole('button',{name:'Get answer'})).toBeEnabled();
  });
});

it('renders a completed direct answer and copyable code instead of plan boxes', async () => {
  const saved: Task = {...task, response_mode:'chat', status:'completed', final_result:'Here is the function.\n```python\ndef add(a, b):\n    return a + b\n```'};
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText}});
  render(<PlanPanel task={saved} autoPlan onReload={vi.fn()}/>);
  expect(screen.getByRole('region',{name:'Assistant answer'})).toHaveTextContent('Here is the function.');
  expect(screen.queryByText('Assumptions')).not.toBeInTheDocument();
  expect(screen.queryByText('EXPECTED OUTPUT')).not.toBeInTheDocument();
  expect(screen.queryByText('Thinking…')).not.toBeInTheDocument();
  expect(screen.queryByText('Ready to help.')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Copy code'}));
  await waitFor(()=>expect(writeText).toHaveBeenCalledWith('def add(a, b):\n    return a + b\n'));
  expect(screen.getByText('Code shown only · not executed')).toBeInTheDocument();
});
