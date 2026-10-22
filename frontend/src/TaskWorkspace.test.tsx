import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import TaskWorkspace from './TaskWorkspace';
import { createTask, getTask, listTasks } from './api/client';

// Explicit test fixtures only. No application demo data.
const task = { id: '00000000-0000-4000-8000-000000000001', description: 'My saved task', status: 'pending',
  created_at: '2026-10-19T12:00:00Z', updated_at: '2026-10-19T12:00:00Z', plan: null, final_result: null, error: null };
const event = { id: 1, task_id: task.id, event_type: 'task_created', status: 'pending',
  message: 'Task saved. Execution has not started.', created_at: task.created_at };
const reply = (data: unknown) => ({ ok: true, json: async () => data });
function server() {
  let saved = false;
  const fetchMock = vi.fn().mockImplementation(async (url: string, options?: RequestInit) => {
    if (options?.method === 'POST') { saved = true; return reply(task); }
    if (url.includes('/history')) return reply({ events: [event] });
    if (url.endsWith('/turns')) return reply({tasks:[task],limit:1,offset:0});
    if (url.includes('?limit')) return reply({ tasks: saved ? [task] : [], limit: 20, offset: 0 });
    return reply(task);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('task workspace', () => {
  it('shows empty state and rejects blank input', async () => {
    const fetchMock = server(); render(<TaskWorkspace />);
    expect(await screen.findByText('No tasks yet')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    expect(screen.getByRole('alert')).toHaveTextContent('between 1 and 5,000');
    expect(fetchMock.mock.calls.some(call => call[1]?.method === 'POST')).toBe(false);
  });
  it('creates, refreshes and displays real response details and history', async () => {
    const fetchMock = server(); render(<TaskWorkspace />);
    await screen.findByText('No tasks yet');
    fireEvent.change(screen.getByLabelText('Task description'), { target: { value: task.description } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    expect(await screen.findByText('Message saved. AgentX is preparing your answer.')).toBeInTheDocument();
    expect(await screen.findByText(event.message)).toBeInTheDocument();
    expect(screen.getByText(task.id)).toBeInTheDocument();
    expect(screen.getByLabelText('Task description')).toHaveValue('');
    await waitFor(() => expect(fetchMock.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(2));
  });
  it('shows loading and prevents duplicate submissions', async () => {
    let resolvePost!: (value: unknown) => void;
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, options?: RequestInit) => options?.method === 'POST'
      ? new Promise(resolve => { resolvePost = resolve; })
      : Promise.resolve(url.includes('/history') ? reply({ events: [event] }) : url.endsWith('/turns') ? reply({tasks:[task],limit:1,offset:0}) : url.includes('?') ? reply({ tasks: [], limit: 20, offset: 0 }) : reply(task))));
    render(<TaskWorkspace />);
    await screen.findByText('No tasks yet');
    fireEvent.change(screen.getByLabelText('Task description'), { target: { value: 'Save this' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    expect(screen.getByRole('button', { name: 'Saving task…' })).toBeDisabled();
    resolvePost(reply(task));
    await screen.findByText(event.message);
  });
  it('retains input and shows backend errors on failed creation', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (_url: string, options?: RequestInit) => options?.method === 'POST'
      ? { ok: false, status: 503, json: async () => ({ error: { message: 'Task storage is unavailable.' } }) }
      : reply({ tasks: [], limit: 20, offset: 0 })));
    render(<TaskWorkspace />); await screen.findByText('No tasks yet');
    fireEvent.change(screen.getByLabelText('Task description'), { target: { value: 'Keep my text' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Task storage is unavailable.');
    expect(screen.getByLabelText('Task description')).toHaveValue('Keep my text');
    expect(screen.queryByText(/Task saved as pending/)).not.toBeInTheDocument();
  });
  it('recovers from list failure using retry', async () => {
    let listCalls = 0;
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/health')) return reply({ status: 'ok', service: 'agentx-api', database: 'ok', phase: 1 });
      if (listCalls++ === 0) throw new TypeError('Failed to fetch');
      return reply({ tasks: [], limit: 20, offset: 0 });
    }));
    render(<TaskWorkspace />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach the backend');
    fireEvent.click(screen.getByRole('button', { name: 'Retry tasks' }));
    expect(await screen.findByText('No tasks yet')).toBeInTheDocument();
  });
  it('shows detail failures and supports retry', async () => {
    let fail = true;
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('?')) return reply({ tasks: [task], limit: 20, offset: 0 });
      if (fail) throw new Error('Details unavailable');
      return url.includes('/history') ? reply({ events: [event] }) : url.endsWith('/turns') ? reply({tasks:[task],limit:1,offset:0}) : reply(task);
    }));
    render(<TaskWorkspace />);
    fireEvent.click(await screen.findByRole('button', { name: /My saved task/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Details unavailable');
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry details' }));
    await waitFor(() => expect(screen.getByText(event.message)).toBeInTheDocument());
  });
  it('rejects malformed data and does not turn HTTP failure into success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply({ tasks: [{}], limit: 20, offset: 0 })));
    await expect(listTasks()).rejects.toThrow('invalid task response');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply({ id: 'wrong' })));
    await expect(getTask(task.id)).rejects.toThrow('invalid task response');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => { throw new Error(); } }));
    await expect(createTask('Save')).rejects.toThrow('HTTP 503');
  });
});
