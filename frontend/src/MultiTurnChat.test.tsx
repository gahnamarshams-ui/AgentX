import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import TaskWorkspace from './TaskWorkspace';
import type { Task } from './api/client';

const rootId='00000000-0000-4000-8000-000000000001';
const childId='00000000-0000-4000-8000-000000000002';
const reply=(data:unknown)=>({ok:true,json:async()=>data});
function server(clarification=false) {
  const tasks:Task[]=[];
  const finish=new Map<string,()=>void>();
  let failFollowup=false;
  const fetch=vi.fn(async(url:string,options?:RequestInit)=>{
    const path=new URL(url).pathname;
    if(path.endsWith('/health'))return reply({status:'ok',service:'agentx-api',database:'ok',phase:1});
    if(options?.method==='POST'&&path==='/api/tasks') {
      const body=JSON.parse(String(options.body));
      if(body.conversation_id&&failFollowup)return {ok:false,status:503,json:async()=>({error:{message:'Storage unavailable'}})};
      const task:Task={id:tasks.length?childId:rootId,description:body.description,conversation_id:body.conversation_id||null,
        status:'pending',plan:null,final_result:null,error:null,created_at:`2026-10-21T10:00:0${tasks.length}Z`,updated_at:'2026-10-21T10:00:00Z'};
      tasks.push(task);return reply({...task});
    }
    const task=tasks.find(t=>path.includes(t.id));
    if(options?.method==='POST'&&path.endsWith('/respond')&&task) {
      if(clarification && options.body) {
        task.status='blocked';task.response_mode='chat';task.final_result='No files created. Functionality: NOT TESTED.';
        return reply({...task});
      }
      task.status='planning';
      return new Promise(resolve=>finish.set(task.id,()=>{
        task.status=clarification?'awaiting_user_input':'completed';task.response_mode='chat';task.final_result=clarification?'Which storage should I use?':task.id===rootId?'Blue remembered.':'Your favorite color is blue.';
        resolve(reply({...task}));
      }));
    }
    if(path.endsWith('/turns'))return reply({tasks:tasks.map(t=>({...t})),limit:tasks.length,offset:0});
    if(path.endsWith('/history'))return reply({events:[]});
    if(task)return reply({...task});
    return reply({tasks:tasks.filter(t=>!t.conversation_id).map(t=>({...t})),limit:20,offset:0});
  });
  vi.stubGlobal('fetch',fetch);
  return {fetch,finish,failFollowup:()=>{failFollowup=true;}};
}
function send(text:string) {
  fireEvent.change(screen.getByLabelText('Task description'),{target:{value:text}});
  fireEvent.keyDown(screen.getByLabelText('Task description'),{key:'Enter'});
}

describe('persisted multi-turn chat',()=>{
  it('continues clarification on the original task and displays blocked evidence',async()=>{
    const backend=server(true);render(<TaskWorkspace/>);await screen.findByText('No tasks yet');
    send('Build a task management website.');
    await waitFor(()=>expect(backend.finish.has(rootId)).toBe(true));backend.finish.get(rootId)!();
    await screen.findByText('Which storage should I use?');
    await waitFor(()=>expect(screen.getByRole('button',{name:'Send task'})).toBeEnabled());
    send('Use localStorage.');
    await screen.findByText('No files created. Functionality: NOT TESTED.');
    const posts=backend.fetch.mock.calls.filter(([,o])=>o?.method==='POST');
    expect(posts.map(([u])=>new URL(u).pathname)).toEqual(['/api/tasks',`/api/tasks/${rootId}/respond`,`/api/tasks/${rootId}/respond`]);
    expect(JSON.parse(String(posts[2][1]?.body))).toEqual({answer:'Use localStorage.'});
    expect(screen.getAllByRole('article',{name:'Your task'})).toHaveLength(1);
  });
  it('runs two sequential turns, blocks duplicate sends, and restores both after reload',async()=>{
    const backend=server();const page=render(<TaskWorkspace/>);
    await screen.findByText('No tasks yet');
    send('Remember my favorite color is blue.');
    fireEvent.keyDown(screen.getByLabelText('Task description'),{key:'Enter'});
    await waitFor(()=>expect(backend.finish.has(rootId)).toBe(true));
    expect(screen.getByRole('button',{name:'AgentX is responding…'})).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText('Task description'),{key:'Enter'});
    expect(backend.fetch.mock.calls.filter(([u,o])=>o?.method==='POST'&&new URL(u).pathname==='/api/tasks')).toHaveLength(1);
    backend.finish.get(rootId)!();
    await screen.findByText('Blue remembered.');
    await waitFor(()=>expect(screen.getByRole('button',{name:'Send task'})).toBeEnabled());
    send('What is my favorite color?');
    await waitFor(()=>expect(backend.finish.has(childId)).toBe(true));
    expect(screen.getAllByRole('article',{name:'Your task'})).toHaveLength(2);
    expect(screen.getByText('Blue remembered.')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText('Task description'),{key:'Enter'});
    backend.finish.get(childId)!();
    await screen.findByText('Your favorite color is blue.');
    await waitFor(()=>expect(screen.getByRole('button',{name:'Send task'})).toBeEnabled());
    const posts=backend.fetch.mock.calls.filter(([,o])=>o?.method==='POST');
    expect(posts).toHaveLength(4);
    expect(JSON.parse(String(posts[2][1]?.body))).toEqual({description:'What is my favorite color?',conversation_id:rootId});
    expect(posts.map(([u])=>new URL(u).pathname)).toEqual(['/api/tasks',`/api/tasks/${rootId}/respond`,'/api/tasks',`/api/tasks/${childId}/respond`]);
    page.unmount();render(<TaskWorkspace/>);
    fireEvent.click(await screen.findByRole('button',{name:'Remember my favorite color is blue.'}));
    await screen.findByText('Your favorite color is blue.');
    expect(screen.getByText('Blue remembered.')).toBeInTheDocument();
    expect(screen.getAllByRole('article',{name:'Your task'})).toHaveLength(2);
    expect(backend.fetch.mock.calls.filter(([,o])=>o?.method==='POST')).toHaveLength(4);
    expect(backend.fetch.mock.calls.some(([u])=>/approval|orchestrate/.test(u))).toBe(false);
  });

  it('keeps earlier turns and the draft on follow-up creation failure; rejects empty input',async()=>{
    const backend=server();render(<TaskWorkspace/>);await screen.findByText('No tasks yet');
    send('Remember my favorite color is blue.');
    await waitFor(()=>expect(backend.finish.has(rootId)).toBe(true));backend.finish.get(rootId)!();
    await screen.findByText('Blue remembered.');
    await waitFor(()=>expect(screen.getByRole('button',{name:'Send task'})).toBeEnabled());
    send('   ');
    expect(screen.getByRole('alert')).toHaveTextContent('between 1 and 5,000');
    backend.failFollowup();send('Keep my follow-up draft');
    expect(await screen.findByRole('alert')).toHaveTextContent('Storage unavailable');
    expect(screen.getByLabelText('Task description')).toHaveValue('Keep my follow-up draft');
    expect(screen.getByText('Blue remembered.')).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'Send task'})).toBeEnabled();
  });
});
