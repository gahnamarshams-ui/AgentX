import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import TaskWorkspace from './TaskWorkspace';

const original = { id:'00000000-0000-4000-8000-000000000001', description:'Original request', title:null as string|null, archived:false,
  status:'pending', created_at:'2026-10-21T06:00:00Z', updated_at:'2026-10-21T06:00:00Z', plan:null, final_result:null, error:null };
const reply = (data: unknown) => ({ok:true,json:async()=>data});
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function() { this.setAttribute('open',''); };
  HTMLDialogElement.prototype.close = function() { this.removeAttribute('open'); };
});
function server() {
  let task = {...original}; let deleted = false; let fail = false;
  const fetch = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/health')) return reply({status:'ok',database:'ok',service:'agentx-api',phase:1});
    if (options?.method === 'POST' && url.endsWith('/conversation')) {
      if (fail) return {ok:false,status:503,json:async()=>({error:{message:'Storage unavailable'}})};
      task = {...task,...JSON.parse(String(options.body))}; return reply({...task});
    }
    if (options?.method === 'DELETE') {deleted = true; return reply({deleted:true,id:task.id});}
    if (url.includes('?')) {
      const p = new URL(url).searchParams;
      const archived = p.get('archived') === 'true'; const q = p.get('q') || '';
      return reply({tasks: !deleted && archived === task.archived && `${task.title} ${task.description} saved history`.toLowerCase().includes(q.toLowerCase()) ? [{...task}] : [],limit:20,offset:0});
    }
    if (url.endsWith('/history')) return reply({events:[]});
    if (url.endsWith('/turns')) return reply({tasks:[{...task}],limit:1,offset:0});
    return reply({...task});
  });
  vi.stubGlobal('fetch', fetch);
  return {fetch, fail:()=>{fail=true;}};
}
function actions(title: string) {fireEvent.click(screen.getByText('•••')); return screen.getByLabelText(`Conversation actions for ${title}`).parentElement!;}

describe('persistent conversation management', () => {
  it('renames through the backend and restores the title after remount', async () => {
    const {fetch} = server(); const page = render(<TaskWorkspace/>);
    await screen.findByRole('button',{name:'Original request'});
    fireEvent.click(within(actions('Original request')).getByRole('button',{name:'Rename'}));
    fireEvent.change(screen.getByLabelText('Conversation title'),{target:{value:'Renamed chat'}});
    fireEvent.click(screen.getByRole('button',{name:'Save title'}));
    await screen.findByRole('button',{name:'Renamed chat'});
    page.unmount(); render(<TaskWorkspace/>);
    expect(await screen.findByRole('button',{name:'Renamed chat'})).toBeInTheDocument();
    expect(fetch.mock.calls.some(([,o])=>o?.body === JSON.stringify({title:'Renamed chat'}))).toBe(true);
  });
  it('archives, separates archived history and restores a conversation', async () => {
    server(); render(<TaskWorkspace/>); await screen.findByRole('button',{name:'Original request'});
    fireEvent.click(within(actions('Original request')).getByRole('button',{name:'Archive'}));
    await screen.findByText('No tasks yet');
    fireEvent.click(screen.getByRole('button',{name:'Archived'}));
    await screen.findByRole('button',{name:'Original request'});
    fireEvent.click(within(actions('Original request')).getByRole('button',{name:'Restore'}));
    await screen.findByText('No archived chats');
    fireEvent.click(screen.getByRole('button',{name:'Active'}));
    expect(await screen.findByRole('button',{name:'Original request'})).toBeInTheDocument();
  });
  it('requires confirmation before deleting and reloads without misleading history', async () => {
    const {fetch} = server(); render(<TaskWorkspace/>); await screen.findByRole('button',{name:'Original request'});
    fireEvent.click(within(actions('Original request')).getByRole('button',{name:'Delete'}));
    expect(screen.getByRole('dialog')).toHaveTextContent('Workspace files are kept');
    expect(fetch.mock.calls.some(([,o])=>o?.method === 'DELETE')).toBe(false);
    fireEvent.click(screen.getByRole('button',{name:'Cancel'}));
    fireEvent.click(within(actions('Original request')).getByRole('button',{name:'Delete'}));
    fireEvent.click(screen.getByRole('button',{name:'Delete permanently'}));
    await screen.findByText('No tasks yet');
    expect(fetch.mock.calls.filter(([,o])=>o?.method === 'DELETE')).toHaveLength(1);
  });
  it('searches available history on the server and shows honest empty results', async () => {
    const {fetch} = server(); render(<TaskWorkspace/>); await screen.findByRole('button',{name:'Original request'});
    fireEvent.change(screen.getByLabelText('Search conversations'),{target:{value:'saved history'}});
    await waitFor(()=>expect(fetch.mock.calls.some(([u])=>u.includes('q=saved+history'))).toBe(true));
    expect(await screen.findByRole('button',{name:'Original request'})).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search conversations'),{target:{value:'missing'}});
    expect(await screen.findByText('No matching conversations')).toBeInTheDocument();
  });
  it('keeps a failed rename dialog open and preserves the original title', async () => {
    const backend=server(); render(<TaskWorkspace/>); await screen.findByRole('button',{name:'Original request'}); backend.fail();
    fireEvent.click(within(actions('Original request')).getByRole('button',{name:'Rename'}));
    fireEvent.change(screen.getByLabelText('Conversation title'),{target:{value:'New'}});
    fireEvent.click(screen.getByRole('button',{name:'Save title'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('Storage unavailable');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'Original request'})).toBeInTheDocument();
  });
});
