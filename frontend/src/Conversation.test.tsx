import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import TaskWorkspace from './TaskWorkspace';
import Composer from './Composer';
import Markdown from './Markdown';
const task = { id: '00000000-0000-4000-8000-000000000001', description: 'A real task', title:null, archived:false, status: 'pending', created_at: '2026-10-20T05:00:00Z', updated_at: '2026-10-20T05:00:00Z', plan: null, final_result: null, error: null };
const reply = (data: unknown) => ({ ok: true, json: async () => data });
function server(existing = false) {
  let saved = existing;
  const fetch = vi.fn().mockImplementation(async (url: string, options?: RequestInit) => {
    if (url.includes('/health')) return reply({ status: 'ok', service: 'agentx-api', database: 'ok', phase: 1 });
    if (options?.method === 'POST') { saved = true; return reply(task); }
    if (url.includes('/history')) return reply({ events: [{ id: 1, task_id: task.id, status: 'pending', event_type: 'task_created', message: 'Task saved.', created_at: task.created_at }] });
    if (url.endsWith('/turns')) return reply({tasks:[task],limit:1,offset:0});
    if (url.includes('?')) { const q = new URL(url).searchParams.get('q') || ''; return reply({ tasks: saved && task.description.toLowerCase().includes(q) ? [task] : [], limit: 20, offset: 0 }); }
    return reply(task);
  });
  vi.stubGlobal('fetch', fetch); return fetch;
}

describe('conversation experience', () => {
  it('explicit workspace mode requests a real execution plan without approving it', async () => {
    const fetch = server(); render(<TaskWorkspace />); await screen.findByText('No tasks yet');
    fireEvent.click(screen.getByRole('checkbox', {name:'Prepare an approved workspace task'}));
    fireEvent.change(screen.getByLabelText('Task description'), {target:{value:'A real task'}});
    fireEvent.keyDown(screen.getByLabelText('Task description'), {key:'Enter'});
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => url.endsWith('/orchestrate'))).toBe(true));
    expect(fetch.mock.calls.some(([url]) => url.endsWith('/respond') || url.endsWith('/approval'))).toBe(false);
  });
  it('populates a suggestion without creating a fake task', async () => {
    const fetch = server(); render(<TaskWorkspace />);
    await screen.findByText('No tasks yet');
    fireEvent.click(screen.getByRole('button', { name: /Plan a project/ }));
    expect(screen.getByLabelText('Task description')).toHaveValue('Help me plan a project. My goal is ');
    expect(screen.getByLabelText('Task description')).toHaveFocus();
    expect(fetch.mock.calls.some(call => call[1]?.method === 'POST')).toBe(false);
  });
  it('submits with Enter, keeps the real user message and automatically requests a plan without execution', async () => {
    const fetch = server(); render(<TaskWorkspace />); await screen.findByText('No tasks yet');
    fireEvent.change(screen.getByLabelText('Task description'), { target: { value: 'A real task' } });
    fireEvent.keyDown(screen.getByLabelText('Task description'), { key: 'Enter' });
    const userMessage = await screen.findByRole('article', { name: 'Your task' });
    expect(userMessage).toHaveTextContent('A real task');
    expect(await screen.findByRole('button', { name: 'Get answer' })).toBeInTheDocument();
    const posts = fetch.mock.calls.filter(call => call[1]?.method === 'POST');
    expect(posts).toHaveLength(2); expect(posts[0][0]).toBe('http://localhost:8000/api/tasks');
    expect(posts[1][0]).toBe(`http://localhost:8000/api/tasks/${task.id}/respond`);
    expect(fetch.mock.calls.some(([url]) => /orchestrate|approval/.test(url))).toBe(false);
  });
  it('filters real history and reopens a saved conversation', async () => {
    server(true); render(<TaskWorkspace />); await screen.findByRole('button', { name: 'A real task' });
    fireEvent.click(screen.getByRole('button', { name: 'Task History' }));
    fireEvent.change(screen.getByLabelText('Search task history'), { target: { value: 'unmatched' } });
    expect(await screen.findByText('No tasks match your search.')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search task history'), { target: { value: 'real' } });
    fireEvent.click(await within(screen.getByRole('main')).findByRole('button', { name: /A real task/ }));
    expect(await screen.findByRole('article', { name: 'Your task' })).toHaveTextContent('A real task');
    expect(screen.getByText('Task activity')).toBeInTheDocument();
  });
  it('new task clears selection and preserves the unsent draft', async () => {
    server(true); render(<TaskWorkspace />);
    fireEvent.click(await screen.findByRole('button', { name: 'A real task' }));
    await screen.findByRole('article', { name: 'Your task' });
    fireEvent.change(screen.getByLabelText('Task description'), { target: { value: 'My unsent draft' } });
    fireEvent.click(screen.getByRole('button', { name: 'New Chat' }));
    expect(screen.getByRole('heading', { name: 'What’s the goal?' })).toBeInTheDocument();
    expect(screen.getByLabelText('Task description')).toHaveValue('My unsent draft');
  });
  it('shows honest tools and approvals states with no execution requests', async () => {
    const fetch = server(); render(<TaskWorkspace />); await screen.findByText('No tasks yet');
    fireEvent.click(screen.getByRole('button', { name: 'Tools' }));
    expect(screen.getByText(/Approved tools: read_text/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approvals' }));
    expect(screen.getByText(/Open a workspace task to review its exact saved tool calls/)).toBeInTheDocument();
    expect(fetch.mock.calls.some(call => call[1]?.method === 'POST')).toBe(false);
  });
  it('shows actual connection information in read-only settings', async () => {
    server(); render(<TaskWorkspace />); await screen.findByText('No tasks yet');
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    expect(screen.getByText('http://localhost:8000')).toBeInTheDocument();
    expect(screen.getByText(/Managed on the backend/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/API key/)).not.toBeInTheDocument();
  });
  it('collapses the desktop sidebar', async () => {
    server(); render(<TaskWorkspace />); await screen.findByText('No tasks yet');
    fireEvent.click(screen.getByRole('button', { name: 'Toggle sidebar' }));
    expect(screen.getByRole('button', { name: 'Toggle sidebar' })).toHaveAttribute('aria-expanded', 'false');
  });
  it('opens mobile navigation, traps focus and closes with Escape', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })));
    server(); render(<TaskWorkspace />); await screen.findByText('No tasks yet');
    fireEvent.click(screen.getByRole('button', { name: 'Toggle sidebar' }));
    const dialog = screen.getByRole('dialog', { name: 'Main navigation' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const close = within(dialog).getByRole('button', { name: 'Close navigation' });
    await waitFor(() => expect(close).toHaveFocus());
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
    expect(within(dialog).getByRole('button', { name: 'Settings' })).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Toggle sidebar' })).toHaveFocus());
  });
});

describe('composer keyboard behavior', () => {
  function composer() { const submit = vi.fn(); render(<Composer value="A goal" onChange={vi.fn()} onSubmit={submit} submitting={false} error="" hasTask={false} />); return submit; }
  it('uses Shift+Enter for a newline without submitting', () => {
    const submit = composer(); fireEvent.keyDown(screen.getByLabelText('Task description'), { key: 'Enter', shiftKey: true }); expect(submit).not.toHaveBeenCalled();
  });
  it('does not send while an IME composition is active', () => {
    const submit = composer(); fireEvent.keyDown(screen.getByLabelText('Task description'), { key: 'Enter', isComposing: true }); expect(submit).not.toHaveBeenCalled();
  });
  it('prevents send during a pending request and retains validation', () => {
    const submit = vi.fn(); render(<Composer value="A goal" onChange={vi.fn()} onSubmit={submit} submitting={true} error="Request failed" hasTask={false} />);
    expect(screen.getByRole('button', { name: 'Saving task…' })).toBeDisabled();
    expect(screen.getByLabelText('Task description')).toHaveAttribute('maxlength', '5000');
    expect(screen.getByRole('alert')).toHaveTextContent('Request failed');
    fireEvent.keyDown(screen.getByLabelText('Task description'), { key: 'Enter' }); expect(submit).not.toHaveBeenCalled();
  });
});

describe('untrusted Markdown', () => {
  it('renders headings, code and lists as semantic content', () => {
    render(<Markdown>{'## A plan\n\n- First\n- Second\n\n```python\nprint("hello")\n```'}</Markdown>);
    expect(screen.getByRole('heading', { name: 'A plan' })).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByText('print("hello")').tagName).toBe('CODE');
  });
  it('blocks raw HTML, unsafe link schemes and remote images', () => {
    const { container } = render(<Markdown>{'<script>alert(1)</script>\n\n[danger](javascript:alert%281%29)\n\n![tracking](https://example.com/pixel)'}</Markdown>);
    expect(container.querySelector('script')).toBeNull(); expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });
});
