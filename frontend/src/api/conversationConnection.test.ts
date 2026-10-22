import { describe, expect, it, vi } from 'vitest';
import { deleteConversation, updateConversation } from './client';
const id = '00000000-0000-4000-8000-000000000001';
const reply = (data: unknown) => ({ok:true,json:async()=>data});
describe('conversation connection diagnosis', () => {
  it('identifies the outdated backend after a browser-blocked DELETE preflight', async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValue(reply({paths:{'/api/tasks/{task_id}':{get:{}}}}));
    vi.stubGlobal('fetch', fetch);
    await expect(deleteConversation(id)).rejects.toThrow('backend is outdated');
    expect(fetch.mock.calls[1][0]).toBe('http://localhost:8000/openapi.json');
    expect(fetch.mock.calls.filter(([,o])=>o?.method === 'DELETE')).toHaveLength(1);
    expect(fetch.mock.calls[1][1].method).toBeUndefined(); // Diagnostic GET; no write retry.
  });
  it('keeps connection errors honest when the updated endpoint exists', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError()).mockResolvedValue(reply({paths:{'/api/tasks/{task_id}':{delete:{}}}})));
    await expect(deleteConversation(id)).rejects.toThrow('Cannot reach the backend');
  });
  it('retains the original error if schema inspection is unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError()));
    await expect(deleteConversation(id)).rejects.toThrow('Cannot reach the backend');
  });
  it('diagnoses missing rename/archive routes without retrying the mutation', async () => {
    const fetch=vi.fn().mockResolvedValueOnce({ok:false,status:404,json:async()=>({error:{message:'Not Found'}})}).mockResolvedValue(reply({paths:{}}));
    vi.stubGlobal('fetch', fetch);
    await expect(updateConversation(id,{title:'New title'})).rejects.toThrow('missing conversation-management endpoints');
    expect(fetch.mock.calls.filter(([,o])=>o?.method === 'POST')).toHaveLength(1);
  });
});
