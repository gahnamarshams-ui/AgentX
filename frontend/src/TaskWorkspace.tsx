import { useCallback, useEffect, useRef, useState } from 'react';
import { motion, useReducedMotion } from 'framer-motion';
import PlanPanel from './PlanPanel';
import ConversationMenu, { conversationTitle } from './ConversationMenu';
import { useTheme } from './theme';
import ThemeStudio from './ThemeStudio';
import { HeaderThemeControl } from './ThemeControl';
import Composer from './Composer';
import Icon, { AgentMark } from './Icon';
import type { IconName } from './Icon';
import { apiBaseUrl, createTask, generateAnswer, getHealth, getHistory, getTask, getTurns, listTasks } from './api/client';
import type { HistoryEvent, Task, TaskStatus } from './api/client';

type View = 'chat' | 'history' | 'tools' | 'approvals' | 'settings';
const navigation: { id: View; label: string; icon: IconName }[] = [
  { id: 'chat', label: 'Chat', icon: 'chat' }, { id: 'history', label: 'Task History', icon: 'history' },
  { id: 'tools', label: 'Tools', icon: 'tools' }, { id: 'approvals', label: 'Approvals', icon: 'shield' },
];
const labels: Record<TaskStatus, string> = { pending: 'Pending', planning: 'Planning', awaiting_user_input: 'Awaiting your input', awaiting_approval: 'Plan ready', running: 'Running', blocked: 'Blocked', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled' };
const suggestions: { title: string; subtitle: string; icon: IconName; prompt: string }[] = [
  { title: 'Plan a project', subtitle: 'Turn an idea into next steps', icon: 'new', prompt: 'Help me plan a project. My goal is ' },
  { title: 'Explore a topic', subtitle: 'Find a clear starting point', icon: 'compass', prompt: 'Create a research plan for ' },
  { title: 'Learn something new', subtitle: 'Build a path from the basics', icon: 'book', prompt: 'Create a learning plan for ' },
  { title: 'Work through a problem', subtitle: 'Break down the moving parts', icon: 'spark', prompt: 'Help me break down this problem: ' },
];
const message = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Please retry.';
const formatDate = (value: string) => new Date(value).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
function Badge({ status }: { status: TaskStatus }) { return <span className={`badge badge-${status}`}><span className="status-dot" />{labels[status]}</span>; }

export default function TaskWorkspace() {
  const theme = useTheme();
  const [view, setView] = useState<View>('chat');
  const [sidebar, setSidebar] = useState(true);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(() => window.matchMedia?.('(max-width: 760px)').matches ?? false);
  const [description, setDescription] = useState('');
  const [workspaceMode, setWorkspaceMode] = useState(false);
  const [workspaceTaskId, setWorkspaceTaskId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [autoPlanTaskId, setAutoPlanTaskId] = useState<string | null>(null);
  const submitLock = useRef(false);
  const [formError, setFormError] = useState('');
  const [notice, setNotice] = useState('');
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const selection = useRef(selected); selection.current = selected;
  const [detail, setDetail] = useState<{ task: Task; events: HistoryEvent[] } | null>(null);
  const [turns, setTurns] = useState<{task: Task; events: HistoryEvent[]}[]>([]);
  const [detailError, setDetailError] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailRefresh, setDetailRefresh] = useState(0);
  const [search, setSearch] = useState('');
  const [archived, setArchived] = useState(false);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [connection, setConnection] = useState('Checking backend connection…');
  const [connected, setConnected] = useState<boolean | null>(null);
  const [healthAttempt, setHealthAttempt] = useState(0);
  const sidebarRef = useRef<HTMLElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotion();

  useEffect(() => {
    const media = window.matchMedia?.('(max-width: 760px)');
    const update = () => { setIsMobile(media?.matches ?? false); if (!media?.matches) setMobileOpen(false); };
    media?.addEventListener?.('change', update);
    return () => media?.removeEventListener?.('change', update);
  }, []);

  useEffect(() => {
    const controller = new AbortController(); let active = true;
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    setConnection('Checking backend connection…'); setConnected(null);
    getHealth(controller.signal).then(() => { if (active) { setConnected(true); setConnection('Backend connected · SQLite ready'); } })
      .catch(error => { if (active) { setConnected(false); setConnection(controller.signal.aborted ? 'Connection timed out. Check that the backend is running.' : message(error)); } })
      .finally(() => window.clearTimeout(timeout));
    return () => { active = false; window.clearTimeout(timeout); controller.abort(); };
  }, [healthAttempt]);

  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setLoading(true); setListError('');
    listTasks(controller.signal, {q: search, archived, offset}).then(data => { if (active) { setTasks(previous => offset ? [...previous.filter(t => !data.tasks.some(n => n.id === t.id)), ...data.tasks] : data.tasks); setHasMore(data.tasks.length === 20); } })
      .catch(error => { if (active) setListError(message(error)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [refresh, search, archived, offset]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController(); let active = true;
    setDetailError(''); setDetailLoading(true);
    Promise.all([getTask(selected, controller.signal), getHistory(selected, controller.signal), getTurns(selected, controller.signal)])
      .then(async ([task, history, thread]) => {
        if (!thread.tasks.some(t => t.id === selected) || thread.tasks.some(t => t.id !== selected && t.conversation_id !== selected)) throw new Error('Invalid conversation association. Reload details.');
        const saved = await Promise.all(thread.tasks.map(async turn => ({task:turn,events:turn.id===selected ? history.events : (await getHistory(turn.id,controller.signal)).events})));
        if (active) { setDetail({ task, events: history.events }); setTurns(saved); }
      })
      .catch(error => { if (active) setDetailError(message(error)); })
      .finally(() => { if (active) setDetailLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [selected, detailRefresh]);

  useEffect(() => {
    // A selection starts at the top; long responses can be read without scroll jumps.
    scrollRef.current?.scrollTo?.({ top: 0 });
  }, [selected, view]);
  useEffect(() => {
    if (!mobileOpen) return;
    const frame = window.requestAnimationFrame(() => sidebarRef.current?.querySelector<HTMLButtonElement>('button')?.focus());
    function closeOnEscape(event: KeyboardEvent) { if (event.key === 'Escape' && !(event.target instanceof Element && event.target.closest('dialog'))) { setMobileOpen(false); window.requestAnimationFrame(() => toggleRef.current?.focus()); } }
    document.addEventListener('keydown', closeOnEscape);
    return () => { window.cancelAnimationFrame(frame); document.removeEventListener('keydown', closeOnEscape); };
  }, [mobileOpen]);

  function closeNavigation() { setMobileOpen(false); if (mobileOpen) window.requestAnimationFrame(() => toggleRef.current?.focus()); }
  function navigate(next: View) { setView(next); closeNavigation(); }
  function newTask() {
    setAutoPlanTaskId(null); setSelected(null); setDetail(null); setTurns([]); setDetailError(''); setView('chat'); setNotice(''); setFormError(''); closeNavigation();
    window.requestAnimationFrame(() => document.getElementById('description')?.focus());
  }
  function openTask(task: Task) {
    setAutoPlanTaskId(null);
    setDetail(null); setTurns([]); setDetailError(''); setDetailLoading(true); setSelected(task.id);
    setDetailRefresh(value => value + 1); setView('chat'); closeNavigation();
  }
  const updateTask = useCallback((task: Task) => {
    if (selection.current !== (task.conversation_id || task.id)) return;
    setTurns(previous => previous.map(turn => turn.task.id === task.id ? {...turn,task} : turn));
    setDetail(previous => previous?.task.id === task.id ? { ...previous, task } : previous);
    setTasks(previous => previous.map(item => item.id === task.id ? task : item));
  }, []);
  const reloadDetails = useCallback(() => { setRefresh(value => value + 1); setDetailRefresh(value => value + 1); }, []);

  async function submit() {
    if (submitLock.current || (selected && (detailLoading || turnBusy))) return;
    setFormError(''); setNotice('');
    if (!description.trim() || description.length > 5000) { setFormError('Enter a task description between 1 and 5,000 characters.'); return; }
    submitLock.current = true; setSubmitting(true);
    try {
      if (activeTurn?.response_mode === 'chat' && activeTurn.status === 'awaiting_user_input') {
        const task = await generateAnswer(activeTurn.id, description.trim());
        updateTask(task); setDescription(''); setNotice('Clarification saved on the original task.');
        reloadDetails();
        return;
      }
      const task = await createTask(description.trim(), selected || undefined);
      setArchived(false); setSearch(''); setOffset(0);
      setAutoPlanTaskId(task.id);
      if (!selected && workspaceMode) setWorkspaceTaskId(task.id);
      setDescription(''); setNotice('Message saved. AgentX is preparing your answer.');
      if (selected) {
        setTurns(previous => [...previous,{task,events:[]}]);
      } else {
        setDetail({task,events:[]}); setTurns([{task,events:[]}]); setSelected(task.id);
      }
      setView('chat'); setRefresh(value => value + 1); setDetailRefresh(value => value + 1);
    } catch (error) { setFormError(`${message(error)} If the connection was interrupted, reload tasks before submitting again.`); }
    finally { submitLock.current = false; setSubmitting(false); }
  }

  const activeTask = detail?.task.id === selected ? detail.task : tasks.find(task => task.id === selected);
  const activeTurn = turns.at(-1)?.task;
  const turnBusy = turns.some(turn => ['pending','planning','running'].includes(turn.task.status)) || Boolean(autoPlanTaskId);
  const title = view === 'chat' ? (activeTask ? conversationTitle(activeTask) : 'AgentX') : view === 'history' ? 'Task History' : view === 'tools' ? 'Tools' : view === 'approvals' ? 'Approvals' : 'Settings';
  const matchingTasks = tasks;
  function changedConversation(task: Task) {
    setTasks(previous => previous.map(t => t.id === task.id ? task : t));
    setDetail(previous => previous?.task.id === task.id ? {...previous, task} : previous);
    setOffset(0); setRefresh(v => v + 1);
  }
  function deletedConversation(id: string) {
    setTasks(previous => previous.filter(t => t.id !== id));
    if (selected === id) newTask();
    setOffset(0); setRefresh(v => v + 1);
  }
  function searchConversations(value: string) { setSearch(value); setOffset(0); }
  let baseUrl = '';
  try { baseUrl = apiBaseUrl(); } catch (error) { baseUrl = message(error); }

  const composer = <Composer value={description} onChange={value => { setDescription(value); setFormError(''); setNotice(''); }} onSubmit={() => void submit()} submitting={submitting || Boolean(selected && (detailLoading || turnBusy))} responding={!submitting && turnBusy} error={formError} hasTask={Boolean(selected)} />;

  return <div className={`app-shell${sidebar ? '' : ' sidebar-collapsed'}${mobileOpen ? ' mobile-open' : ''}`}>
    <a className="skip-link" href="#conversation">Skip to conversation</a>
    <aside id="navigation" className="sidebar" ref={sidebarRef} aria-label="Main navigation" role={mobileOpen ? 'dialog' : undefined} aria-modal={mobileOpen || undefined}
      onKeyDown={event => {
        if (!mobileOpen || event.key !== 'Tab' || (event.target instanceof Element && event.target.closest('dialog'))) return;
        const items = Array.from(sidebarRef.current?.querySelectorAll<HTMLElement>('button, input, a[href], summary') || [])
          .filter(el => !el.hasAttribute('disabled') && (!el.closest('details:not([open])') || el.tagName === 'SUMMARY'))
          .sort((a, b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
        const first = items[0], last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
      <div className="sidebar-brand"><AgentMark /><span>AgentX<span className="brand-dot">.</span></span>
        <button className="icon-button mobile-close" aria-label="Close navigation" onClick={() => { closeNavigation(); toggleRef.current?.focus(); }}><Icon name="close" /></button></div>
      <button aria-label="New Chat" className="new-task-button" onClick={newTask} disabled={submitting}><Icon name="new" /><span>New Chat</span><span className="new-task-plus" aria-hidden="true">+</span></button>
      <nav aria-label="Workspace">{navigation.map(item => <button key={item.id} className={`nav-item${view === item.id ? ' active' : ''}`} aria-current={view === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}>
        <Icon name={item.icon} /><span>{item.label}</span>{item.id === 'chat' && <span className="nav-marker" />}</button>)}</nav>
      <div className="sidebar-history"><div className="sidebar-section-title"><span>{archived ? 'Archived chats' : 'Recent tasks'}</span><button className="icon-button" aria-label="Reload tasks" disabled={loading} onClick={() => setRefresh(value => value + 1)}><Icon name="refresh" /></button></div>
        <label className="conversation-search"><span className="sr-only">Search conversations</span><input type="search" value={search} onChange={e => searchConversations(e.target.value)} placeholder="Search conversations…" /></label>
        <div className="conversation-tabs" aria-label="Conversation archive"><button aria-pressed={!archived} onClick={() => {setArchived(false); setOffset(0);}}>Active</button><button aria-pressed={archived} onClick={() => {setArchived(true); setOffset(0);}}>Archived</button></div>
        {loading && <p className="sidebar-note" role="status">Loading tasks…</p>}
        {listError && <div className="sidebar-error" role="alert"><p>{listError}</p><button className="text-action" onClick={() => setRefresh(value => value + 1)}>Retry tasks</button><p className="muted">Previous entries may be out of date.</p></div>}
        {!loading && !listError && tasks.length === 0 && <div className="sidebar-empty"><Icon name="chat" /><p>{search ? 'No matching conversations' : archived ? 'No archived chats' : 'No tasks yet'}</p><span>A new idea is a good place to start.</span></div>}
        <ul className="conversation-list" aria-label="Recent tasks">{tasks.map(task => <li key={task.id}><button className={`conversation-link${selected === task.id && view === 'chat' ? ' selected' : ''}`} aria-current={selected === task.id && view === 'chat' ? 'true' : undefined} onClick={() => openTask(task)}>
          <span className={`tiny-dot dot-${task.status}`} /><span>{conversationTitle(task)}</span></button><ConversationMenu task={task} onChanged={changedConversation} onDeleted={deletedConversation} /></li>)}</ul>
        {hasMore && <button className="text-action" disabled={loading} onClick={() => setOffset(v => v + 20)}>Load more conversations</button>}
      </div>
      <div className="sidebar-bottom"><button className={`nav-item${view === 'settings' ? ' active' : ''}`} onClick={() => navigate('settings')}><Icon name="settings" /><span>Settings</span></button>
        <div className="connection-indicator"><span className={`tiny-dot ${connected === true ? 'online' : connected === false ? 'offline' : ''}`} /><span>{connected === true ? 'Connected to your workspace' : connected === false ? 'Backend unavailable' : 'Connecting to workspace'}</span></div>
        </div>
    </aside>
    {mobileOpen && <button className="sidebar-scrim" aria-label="Dismiss navigation" tabIndex={-1} onClick={closeNavigation} />}
    <div className="main-shell" inert={mobileOpen ? true : undefined}>
      <header className="topbar"><div className="topbar-leading"><button ref={toggleRef} className="icon-button sidebar-toggle" aria-label="Toggle sidebar" aria-controls="navigation" aria-expanded={isMobile ? mobileOpen : sidebar}
        onClick={() => { if (isMobile) setMobileOpen(value => !value); else setSidebar(value => !value); }}><Icon name="panel" /></button>
        <span className="topbar-title" title={title}>{title}</span></div>
        <div className="topbar-actions">{activeTask && view === 'chat' ? <Badge status={(activeTurn || activeTask).status} /> : <span className="mode-badge"><span className="tiny-dot online" />Chat mode</span>}
          <HeaderThemeControl {...theme} />
          <button className="icon-button" aria-label="Check connection" title={connection} onClick={() => setHealthAttempt(value => value + 1)}><Icon name="refresh" /></button></div></header>
      {connected === false && <div className="connection-banner" role="status"><span>{connection}</span><button onClick={() => setHealthAttempt(value => value + 1)}>Retry connection</button></div>}
      <span className="sr-only" role="status">{connected === false ? '' : connection}</span>
      <div className="canvas-scroll" ref={scrollRef}>
        <main id="conversation" tabIndex={-1} className={view === 'chat' ? `conversation-canvas${selected ? '' : ' is-empty'}` : 'utility-canvas'}>
          {view === 'chat' && !selected && <motion.div className="welcome" initial={reducedMotion ? false : { opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .22 }}>
            <h1>What’s the goal?</h1>
            <label><input type="checkbox" checked={workspaceMode} onChange={event => setWorkspaceMode(event.target.checked)} />Prepare an approved workspace task</label>
            {workspaceMode && <p>Requires execution enabled on a supported Windows, macOS or Linux filesystem. Only new text/JSON/CSV files and JSON checks are supported. Web app implementation and application tests are blocked.</p>}
            {composer}
            <div className="suggestions">{suggestions.map(item => <button key={item.title} title={item.subtitle} disabled={submitting} onClick={() => { setDescription(item.prompt); setFormError(''); document.getElementById('description')?.focus(); }}>
              <Icon name={item.icon} /><span>{item.title}</span></button>)}</div>
          </motion.div>}
          {view === 'chat' && selected && <div className="conversation-thread">
            {detailLoading && !detail && <p role="status" className="detail-loading"><span className="spinner" />Loading task details…</p>}
            {detailError && <div role="alert" className="error"><p>{detailError}</p><button className="text-action" onClick={() => setDetailRefresh(value => value + 1)}>Retry details</button></div>}
            {detail && detail.task.id === selected && turns.map(detail => <section key={detail.task.id} aria-label="Conversation turn">
              <div className="message-date">{formatDate(detail.task.created_at)}</div>
              <article className="user-message" aria-label="Your task"><div className="user-bubble">{detail.task.description}</div><span className="user-message-label">You</span></article>
              <PlanPanel workspaceRequested={workspaceTaskId === detail.task.id} autoPlan={autoPlanTaskId === detail.task.id} onAutoPlanStarted={() => setAutoPlanTaskId(null)} task={detail.task} onReload={reloadDetails} onStatusChange={updateTask} />
              <details className="execution-details"><summary><Icon name="history" /><span>Task activity</span><span>{detail.events.length} {detail.events.length === 1 ? 'event' : 'events'}</span><Icon name="chevron" /></summary>
                <div className="task-metadata"><Badge status={detail.task.status} /><dl><dt>Task ID</dt><dd>{detail.task.id}</dd><dt>Created</dt><dd>{formatDate(detail.task.created_at)}</dd><dt>Updated</dt><dd>{formatDate(detail.task.updated_at)}</dd></dl></div>
                {detail.events.length ? <ol className="activity-list">{detail.events.map(event => <li key={event.id}><span className="activity-dot" /><div><p>{event.message}</p><span>{labels[event.status]} · <time dateTime={event.created_at}>{formatDate(event.created_at)}</time></span></div></li>)}</ol> : <p className="muted">No history events recorded.</p>}
              </details>
            </section>)}
          </div>}
          {view === 'history' && <section className="history-view"><p className="eyebrow">YOUR WORKSPACE</p><h1>Task history</h1><p className="muted">Pick up where you left off. Search saved titles, requests, plans, results and activity.</p>
            <label className="search-field"><Icon name="search" /><span className="sr-only">Search task history</span><input value={search} onChange={event => searchConversations(event.target.value)} placeholder="Search your tasks" /></label>
            {loading && <p role="status" className="muted">Refreshing task history…</p>}
            {listError && <div className="error"><p>{listError}</p><button className="text-action" onClick={() => setRefresh(value => value + 1)}>Reload history</button></div>}
            {!loading && !listError && !matchingTasks.length && <p className="utility-empty">{search ? 'No tasks match your search.' : archived ? 'No archived chats.' : 'No saved tasks yet. Start with a new task.'}</p>}
            <ul className="history-list">{matchingTasks.map(task => <li key={task.id}><button onClick={() => openTask(task)}><Icon name="chat" /><span><strong>{conversationTitle(task)}</strong><time dateTime={task.created_at}>{formatDate(task.created_at)}</time></span><Badge status={task.status} /><Icon name="chevron" /></button></li>)}</ul>
          </section>}
          {(view === 'tools' || view === 'approvals') && <section className="unavailable-view"><span className="utility-symbol"><Icon name={view === 'tools' ? 'tools' : 'shield'} /></span><p className="eyebrow">ROOM TO GROW</p>
            <h1>{view === 'tools' ? 'Workspace tools' : 'Workspace approvals'}</h1><p>{view === 'tools' ? 'Approved tools: read_text, write_text (new files only), validate_json. Execution is opt-in and requires a supported Windows, macOS or Linux filesystem. No application test runner or deployment is available.' : 'Open a workspace task to review its exact saved tool calls and file content, then approve or reject that plan. No tool runs before approval.'}</p><button className="text-action" onClick={() => navigate('chat')}>Back to conversation <Icon name="chevron" /></button></section>}
          {view === 'settings' && <section className="settings-view"><p className="eyebrow">YOUR WORKSPACE</p><h1>Settings</h1><p className="muted">Make this workspace feel like yours.</p><ThemeStudio {...theme} />{theme.persistenceError && <p className="error" role="status">{theme.persistenceError}</p>}<dl><dt>Backend</dt><dd>{baseUrl}</dd><dt>Connection</dt><dd>{connection}</dd><dt>AI configuration</dt><dd>Managed on the backend. API keys are never stored in this interface.</dd><dt>Current capabilities</dt><dd>Save tasks, generate plans, and review history.</dd></dl><button className="secondary-button" onClick={() => setHealthAttempt(value => value + 1)}><Icon name="refresh" />Recheck connection</button></section>}
        </main>
      </div>
      {view === 'chat' && <><span className="sr-only" role="status">{notice}</span>{selected && composer}</>}
      {view !== 'chat' && <footer className="utility-footer">AgentX · Thoughtful plans. Deliberate actions.</footer>}
    </div>
  </div>;
}
