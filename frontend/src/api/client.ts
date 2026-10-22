export interface HealthResponse {
  status: 'ok'; service: 'agentx-api'; database: 'ok'; phase: 1;
}

export function apiBaseUrl(value = import.meta.env.VITE_API_BASE_URL): string {
  const raw = value ?? 'http://localhost:8000';
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error('VITE_API_BASE_URL must be an absolute HTTP(S) origin.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('VITE_API_BASE_URL must be an HTTP(S) origin without credentials or a path.');
  }
  return url.origin;
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch(`${apiBaseUrl()}/api/v1/health`, { signal });
  if (!response.ok) throw new Error(`Backend health check failed (HTTP ${response.status}).`);
  const data: unknown = await response.json();
  if (typeof data !== 'object' || data === null || !('status' in data) || data.status !== 'ok'
      || !('database' in data) || data.database !== 'ok' || !('service' in data)
      || data.service !== 'agentx-api' || !('phase' in data) || data.phase !== 1) {
    throw new Error('Backend returned an invalid health response.');
  }
  return data as HealthResponse;
}

export const taskStatuses = ['pending', 'planning', 'awaiting_user_input', 'awaiting_approval', 'running', 'blocked', 'completed', 'failed', 'cancelled'] as const;
export type TaskStatus = typeof taskStatuses[number];
export interface PlanStep { number: number; action: string; purpose: string; expected_output: string; dependencies: number[] }
export interface TaskPlan {
  goal: string; assumptions: string[]; constraints: string[]; steps: PlanStep[];
  clarification_needed: boolean; clarification_questions: string[];
}
const boundedText = (value: unknown, max: number) => typeof value === 'string' && value.trim().length > 0 && value.length <= max;
const textList = (value: unknown) => Array.isArray(value) && value.length <= 10 && value.every(item => boundedText(item, 300));
export function isPlan(value: unknown): value is TaskPlan {
  if (!record(value) || !boundedText(value.goal, 1000) || !textList(value.assumptions) || !textList(value.constraints)
      || typeof value.clarification_needed !== 'boolean' || !textList(value.clarification_questions)
      || !Array.isArray(value.clarification_questions) || value.clarification_needed !== (value.clarification_questions.length > 0)
      || !Array.isArray(value.steps) || value.steps.length < 1 || value.steps.length > 12) return false;
  return value.steps.every((step, index) => record(step) && step.number === index + 1
    && boundedText(step.action, 300) && boundedText(step.purpose, 1000) && boundedText(step.expected_output, 1000)
    && Array.isArray(step.dependencies) && step.dependencies.length <= 11
    && new Set(step.dependencies).size === step.dependencies.length
    && step.dependencies.every(dep => Number.isInteger(dep) && dep >= 1 && dep < index + 1));
}
export interface Task {
  conversation_id?: string | null;
  id: string; description: string; response_mode?: 'chat' | null; title?: string | null; archived?: boolean; status: TaskStatus; created_at: string; updated_at: string;
  plan: TaskPlan | null; final_result: string | null; error: string | null;
}
export interface HistoryEvent {
  id: number; task_id: string; event_type: 'task_created' | 'status_changed';
  status: TaskStatus; message: string; created_at: string;
}
export interface TaskList { tasks: Task[]; limit: number; offset: number }
export interface TaskHistory { events: HistoryEvent[] }
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const date = (value: unknown): value is string => typeof value === 'string' && Number.isFinite(Date.parse(value));
const status = (value: unknown): value is TaskStatus => typeof value === 'string' && taskStatuses.some(item => item === value);
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$/i.test(value);
const nullableText = (value: unknown) => value === null || typeof value === 'string';
function isTask(value: unknown): value is Task {
  return record(value) && uuid(value.id) && typeof value.description === 'string' && status(value.status)
    && (value.conversation_id === undefined || value.conversation_id === null || uuid(value.conversation_id))
    && (value.title === undefined || nullableText(value.title)) && (value.archived === undefined || typeof value.archived === 'boolean')
    && date(value.created_at) && date(value.updated_at) && (value.plan === null || isPlan(value.plan))
    && nullableText(value.final_result) && nullableText(value.error);
}
function isHistory(value: unknown): value is TaskHistory {
  return record(value) && Array.isArray(value.events) && value.events.every(event => record(event)
    && Number.isInteger(event.id) && uuid(event.task_id) && status(event.status)
    && ['task_created', 'status_changed'].includes(String(event.event_type))
    && typeof event.message === 'string' && date(event.created_at));
}
function isList(value: unknown): value is TaskList {
  return record(value) && Array.isArray(value.tasks) && value.tasks.every(isTask)
    && Number.isInteger(value.limit) && Number.isInteger(value.offset);
}

async function outdatedConversationBackend(path: string, method: string | undefined, signal: AbortSignal): Promise<boolean> {
  const route = method === 'DELETE' ? '/api/tasks/{task_id}'
    : path.endsWith('/conversation') ? '/api/tasks/{task_id}/conversation' : path.endsWith('/respond') ? '/api/tasks/{task_id}/respond' : null;
  if (!route) return false;
  try {
    const response = await fetch(`${apiBaseUrl()}/openapi.json`, { signal });
    if (!response.ok) return false;
    const spec: unknown = await response.json();
    if (!record(spec) || !record(spec.paths)) return false;
    const operation = spec.paths[route];
    return !record(operation) || !record(operation[(method || 'GET').toLowerCase()]);
  } catch { return false; } // Preserve the original failure if diagnosis is unavailable.
}

async function request<T>(path: string, validate: (value: unknown) => value is T, options: RequestInit = {}, timeoutMs = 10000): Promise<T> {
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  const external = options.signal;
  if (external?.aborted) controller.abort();
  external?.addEventListener('abort', onAbort, { once: true });
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, { ...options, signal: controller.signal });
    if (!response.ok) {
      let message = `Request failed (HTTP ${response.status}).`;
      try {
        const body: unknown = await response.json();
        if (record(body) && record(body.error) && typeof body.error.message === 'string') message = body.error.message;
      } catch { /* Keep the HTTP error if the server returned a non-JSON error page. */ }
      if (response.status === 404 && path.endsWith('/activity') && message === 'Not Found') {
        message = 'Saved agent activity requires the updated backend. The running server has not loaded the new read-only endpoint yet.';
      }
      if ([404, 405].includes(response.status) && await outdatedConversationBackend(path, options.method, controller.signal)) {
        message = path.endsWith('/respond') ? 'The running backend has not loaded direct answers yet. Restart it with updated code and execution disabled.' : 'The running backend is outdated and is missing conversation-management endpoints. Restart it with the updated code and execution disabled, then retry.';
      }
      throw new Error(message);
    }
    const data: unknown = await response.json();
    if (!validate(data)) throw new Error('Backend returned an invalid task response.');
    return data;
  } catch (error) {
    if (controller.signal.aborted) throw new Error('Request interrupted or timed out. Reload tasks to check the latest saved state before resubmitting.');
    if (error instanceof TypeError) {
      if (await outdatedConversationBackend(path, options.method, controller.signal)) {
        throw new Error('The running backend is outdated and is missing conversation-management endpoints. Its CORS settings may also block deletion. Restart it with the updated code and execution disabled, then retry.');
      }
      throw new Error('Cannot reach the backend. Check the connection and retry.');
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    external?.removeEventListener('abort', onAbort);
  }
}

export const listTasks = (signal?: AbortSignal, filters?: { q: string; archived: boolean; offset: number }) => {
  const query = new URLSearchParams({ limit: '20' });
  query.set('conversations_only', 'true');
  if (filters) { query.set('q', filters.q); query.set('archived', String(filters.archived)); query.set('offset', String(filters.offset)); }
  return request(`/api/tasks?${query}`, isList, { signal }).then(data => {
    if (filters && (filters.q || filters.archived) && data.tasks.some(task => task.archived === undefined)) {
      throw new Error('Conversation search and archive require the updated backend. The running server has not loaded these endpoints yet.');
    }
    return data;
  });
};
export const updateConversation = (id: string, changes: {title?: string; archived?: boolean}) => request(`/api/tasks/${encodeURIComponent(id)}/conversation`, isTask, {
  method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(changes),
});
export const deleteConversation = (id: string) => request(`/api/tasks/${encodeURIComponent(id)}`, (v): v is {deleted: true; id: string} => record(v) && v.deleted === true && v.id === id, {method: 'DELETE'});
export const getTask = (id: string, signal?: AbortSignal) => request(`/api/tasks/${encodeURIComponent(id)}`, isTask, { signal });
export const getHistory = (id: string, signal?: AbortSignal) => request(`/api/tasks/${encodeURIComponent(id)}/history`, isHistory, { signal });
export const getTurns = (id: string, signal?: AbortSignal) => request(`/api/tasks/${encodeURIComponent(id)}/turns`, isList, { signal });
export const createTask = (description: string, conversationId?: string) => request('/api/tasks', isTask, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ description, ...(conversationId ? {conversation_id:conversationId} : {}) }),
});

// Backend deadline is at most 60 seconds; leave time for persistence and network latency.
export const generateAnswer = (id: string, answer?: string) => request(`/api/tasks/${encodeURIComponent(id)}/respond`, isTask, {
  method: 'POST', ...(answer === undefined ? {} : {headers:{'Content-Type':'application/json'}, body:JSON.stringify({answer})}),
}, 150000);

export const generatePlan = (id: string) => request(`/api/tasks/${encodeURIComponent(id)}/plan`, isTask, { method: 'POST' }, 75000);

export interface AgentEvidence { path: string; sha256: string; byte_count: number; content: string | null; checks: string[] }
export interface TextVerification {
  code_executed: false; factual_correctness_verified: false; answer_sha256: string | null;
  checks: { name: string; category: 'integrity' | 'static_code' | 'correctness'; status: 'passed' | 'failed' | 'not_checked'; detail: string }[];
}
export interface AgentState {
  role: 'Orchestrator' | 'Planner' | 'Safety' | 'Executor' | 'Verifier';
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped';
  attempts: number; input_summary: string; output_summary: string; input_steps: number[];
  evidence: AgentEvidence[]; checks: string[]; error: string | null;
  text_verification?: TextVerification | null;
}
export interface ExecutionProgress { task_id: string; phase: string; plan_digest: string | null; agents: AgentState[] }
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(x => typeof x === 'string');
function isTextVerification(v: unknown): v is TextVerification {
  return record(v) && v.code_executed === false && v.factual_correctness_verified === false
    && (v.answer_sha256 === null || typeof v.answer_sha256 === 'string' && /^[a-f0-9]{64}$/.test(v.answer_sha256))
    && Array.isArray(v.checks) && v.checks.every(c => record(c) && typeof c.name === 'string'
      && ['integrity', 'static_code', 'correctness'].includes(String(c.category))
      && ['passed', 'failed', 'not_checked'].includes(String(c.status)) && typeof c.detail === 'string');
}
export function isExecutionProgress(v: unknown): v is ExecutionProgress {
  if (!record(v) || !uuid(v.task_id) || !['pending', 'planning', 'awaiting_user_input', 'awaiting_approval', 'executing', 'verifying', 'blocked', 'completed', 'failed', 'cancelled'].includes(String(v.phase))
    || !(v.plan_digest === null || typeof v.plan_digest === 'string' && /^[a-f0-9]{64}$/.test(v.plan_digest))
    || !Array.isArray(v.agents) || v.agents.length !== 5) return false;
  const roles = ['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier'];
  return v.agents.every((a, i) => record(a) && a.role === roles[i]
    && ['pending', 'running', 'succeeded', 'failed', 'skipped'].includes(String(a.status))
    && Number.isInteger(a.attempts) && Number(a.attempts) >= 0 && Number(a.attempts) <= 3
    && typeof a.input_summary === 'string' && typeof a.output_summary === 'string' && nullableText(a.error)
    && (a.text_verification === undefined || a.text_verification === null || isTextVerification(a.text_verification))
    && Array.isArray(a.input_steps) && a.input_steps.every(n => Number.isInteger(n) && n > 0 && n <= 12)
    && strings(a.checks) && Array.isArray(a.evidence) && a.evidence.every(e => record(e)
      && typeof e.path === 'string' && typeof e.sha256 === 'string' && /^[a-f0-9]{64}$/.test(e.sha256)
      && Number.isInteger(e.byte_count) && Number(e.byte_count) >= 0 && nullableText(e.content) && strings(e.checks)));
}
export const getExecutionProgress = (id: string, signal?: AbortSignal) => request(`/api/tasks/${encodeURIComponent(id)}/activity`, isExecutionProgress, { signal });

export interface WorkspaceRun {
  task_id: string; phase: string; plan_digest: string | null; error: string | null;
  steps: {number: number; status: string; tool: {name: string; path?: string; content?: string | null; required_keys?: string[]}; result: AgentEvidence | null; verification: {passed: boolean; checks: string[]} | null; error: string | null}[];
}
function isWorkspaceRun(v: unknown): v is WorkspaceRun {
  return record(v) && uuid(v.task_id) && typeof v.phase === 'string'
    && (v.plan_digest === null || typeof v.plan_digest === 'string' && /^[a-f0-9]{64}$/.test(v.plan_digest))
    && nullableText(v.error) && Array.isArray(v.steps) && v.steps.every(s => record(s)
      && Number.isInteger(s.number) && typeof s.status === 'string' && record(s.tool) && typeof s.tool.name === 'string'
      && (s.tool.content == null || typeof s.tool.content === 'string')
      && (s.result === null || record(s.result) && typeof s.result.sha256 === 'string')
      && (s.verification === null || record(s.verification) && typeof s.verification.passed === 'boolean' && strings(s.verification.checks)));
}
export const orchestrate = (id: string) => request(`/api/tasks/${encodeURIComponent(id)}/orchestrate`, isWorkspaceRun, {method: 'POST'}, 75000);
export const getWorkspaceRun = (id: string) => request(`/api/tasks/${encodeURIComponent(id)}/execution`, isWorkspaceRun);
export const clarifyWorkspace = (id: string, answer: string) => request(`/api/tasks/${encodeURIComponent(id)}/clarification`, isWorkspaceRun, {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({answer})}, 75000);
export const approveWorkspace = (id: string, plan_digest: string, decision: 'approve' | 'reject') => request(`/api/tasks/${encodeURIComponent(id)}/approval`, isWorkspaceRun, {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({plan_digest, decision})}, 150000);
