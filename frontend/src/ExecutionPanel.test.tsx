import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ExecutionPanel from './ExecutionPanel';
import PlanPanel from './PlanPanel';
import type { Task } from './api/client';

const id = '11111111-1111-4111-8111-111111111111';
const task: Task = {id, description:'Save budget JSON', status:'awaiting_approval', created_at:'2026-10-22T00:00:00Z', updated_at:'2026-10-22T00:00:00Z', plan:null, final_result:null, error:null};
const run = {task_id:id, phase:'awaiting_approval', plan_digest:'a'.repeat(64), error:null,
  steps:[{number:1,status:'pending',tool:{name:'write_text',path:'fest.json',content:'{"budget":25000}',required_keys:[]},result:null,verification:null,error:null}]};
const reply = (data: unknown) => ({ok:true,json:async()=>data});

describe('workspace approval controls', () => {
  it('shows blocked implementation and NOT TESTED without a completion claim', () => {
    render(<PlanPanel task={{...task,response_mode:'chat',status:'blocked',final_result:'No files created. Functionality: NOT TESTED.'}} onReload={vi.fn()} />);
    expect(screen.getByRole('status')).toHaveTextContent('implementation has not completed');
    expect(screen.getByText(/Functionality: NOT TESTED/)).toBeInTheDocument();
    expect(screen.queryByText('Verified result')).not.toBeInTheDocument();
    expect(screen.queryByRole('button',{name:'Get answer'})).not.toBeInTheDocument();
  });
  it('shows exact content and digest and sends approval only after a click', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(reply(run)).mockResolvedValueOnce(reply({...run,phase:'completed'}));
    vi.stubGlobal('fetch', fetch);
    render(<ExecutionPanel task={task} onReload={vi.fn()} />);
    expect(await screen.findByText(/Approval digest/)).toHaveTextContent(run.plan_digest);
    expect(screen.getByText(/"budget":25000/)).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button',{name:'Approve exact workspace actions'}));
    await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({plan_digest:run.plan_digest,decision:'approve'});
  });
  it('supports rejection without inventing successful output', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(reply(run)).mockResolvedValueOnce(reply({...run,phase:'cancelled'}));
    vi.stubGlobal('fetch',fetch);
    render(<ExecutionPanel task={task} onReload={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button',{name:'Reject workspace actions'}));
    expect(await screen.findByText('Saved execution phase: cancelled')).toBeInTheDocument();
    expect(JSON.parse(fetch.mock.calls[1][1].body).decision).toBe('reject');
    expect(screen.getByText('Verification: NOT TESTED')).toBeInTheDocument();
  });
  it('continues the same task after clarification and then requires approval', async () => {
    const fetch=vi.fn().mockResolvedValueOnce(reply({...run,phase:'awaiting_user_input',plan_digest:null,steps:[]})).mockResolvedValueOnce(reply(run));
    vi.stubGlobal('fetch',fetch);
    render(<ExecutionPanel task={{...task,status:'awaiting_user_input'}} onReload={vi.fn()} />);
    fireEvent.change(await screen.findByLabelText('Clarification for the original task'), {target:{value:'Save JSON only'}});
    fireEvent.click(screen.getByRole('button',{name:'Continue original task'}));
    expect(await screen.findByRole('button',{name:'Approve exact workspace actions'})).toBeEnabled();
    expect(fetch.mock.calls[1][0]).toContain(`/api/tasks/${id}/clarification`);
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({answer:'Save JSON only'});
  });
  it('reports failed approval requests without claiming completion', async () => {
    const fetch=vi.fn().mockResolvedValueOnce(reply(run)).mockResolvedValueOnce({ok:false,status:503,json:async()=>({error:{message:'Workspace execution is disabled.'}})});
    vi.stubGlobal('fetch',fetch);
    render(<ExecutionPanel task={task} onReload={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button',{name:'Approve exact workspace actions'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('Workspace execution is disabled.');
    expect(screen.queryByText('Saved execution phase: completed')).not.toBeInTheDocument();
  });
  it('renders saved chat clarification while keeping the task awaiting input', () => {
    render(<PlanPanel task={{...task,response_mode:'chat',status:'awaiting_user_input',final_result:'Which event dates should I use?'}} onReload={vi.fn()} />);
    expect(screen.getByText('Which event dates should I use?')).toBeInTheDocument();
    expect(screen.queryByRole('region',{name:'Verified task result'})).not.toBeInTheDocument();
  });
});
