import { useEffect, useRef, useState } from 'react';
import { deleteConversation, updateConversation } from './api/client';
import type { Task } from './api/client';

export const conversationTitle = (task: Task) => task.title || task.description;

export default function ConversationMenu({ task, onChanged, onDeleted }: {
  task: Task; onChanged: (task: Task) => void; onDeleted: (id: string) => void;
}) {
  const [action, setAction] = useState<'rename' | 'delete' | null>(null);
  const [title, setTitle] = useState(conversationTitle(task).slice(0, 120));
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const menu = useRef<HTMLDetailsElement>(null);
  const trigger = useRef<HTMLElement>(null);
  useEffect(() => {
    if (action && dialog.current && !dialog.current.open) dialog.current.showModal();
  }, [action]);
  function close() { dialog.current?.close(); setAction(null); setError(''); trigger.current?.focus(); }
  function choose(next: 'rename' | 'delete') {
    if (menu.current) menu.current.open = false;
    setTitle(conversationTitle(task).slice(0, 120)); setError(''); setAction(next);
  }
  async function save(kind: 'rename' | 'archive' | 'delete') {
    if (lock.current) return;
    if (kind === 'rename' && !title.trim()) { setError('Enter a conversation title.'); return; }
    lock.current = true; setBusy(true); setError('');
    try {
      if (kind === 'delete') { await deleteConversation(task.id); onDeleted(task.id); }
      else onChanged(await updateConversation(task.id, kind === 'rename' ? {title: title.trim()} : {archived: !task.archived}));
      close(); if (menu.current) menu.current.open = false;
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not save conversation changes.'); }
    finally { lock.current = false; setBusy(false); }
  }
  return <>
    <details className="conversation-menu" ref={menu}>
      <summary ref={trigger} aria-label={`Conversation actions for ${conversationTitle(task)}`}>•••</summary>
      <div className="conversation-menu-items">
        <button onClick={() => choose('rename')} disabled={busy}>Rename</button>
        <button onClick={() => void save('archive')} disabled={busy}>{busy ? 'Saving…' : task.archived ? 'Restore' : 'Archive'}</button>
        <button onClick={() => choose('delete')} disabled={busy}>Delete</button>
        {error && !action && <p role="alert">{error}</p>}
      </div>
    </details>
    {action && <dialog ref={dialog} className="conversation-dialog" aria-labelledby={`dialog-${task.id}`}
      onCancel={e => { e.preventDefault(); if (!busy) close(); }}>
      <form onSubmit={e => { e.preventDefault(); void save(action); }}>
        <h2 id={`dialog-${task.id}`}>{action === 'rename' ? 'Rename conversation' : 'Delete conversation?'}</h2>
        {action === 'rename' ? <label>Conversation title<input autoFocus maxLength={120} value={title} disabled={busy} onChange={e => setTitle(e.target.value)} /></label>
          : <p>This permanently deletes “{conversationTitle(task)}” and its saved plan, history, approvals and execution records. Workspace files are kept. This cannot be undone.</p>}
        {error && <p role="alert" className="error">{error}</p>}
        <div className="dialog-actions"><button type="button" className="secondary-button" disabled={busy} onClick={close}>Cancel</button>
          <button className="secondary-button" disabled={busy}>{busy ? 'Saving…' : action === 'rename' ? 'Save title' : 'Delete permanently'}</button></div>
      </form>
    </dialog>}
  </>;
}
