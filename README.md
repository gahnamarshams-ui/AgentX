# AgentX — Conversational Workspace

See [the execution implementation report](IMPLEMENTATION_REPORT.md) for the current clarification/approval workflow, exact test results, changed files, and execution limitations. Execution remains disabled by default; the existing tools use native Windows or POSIX protections and cannot implement or test a full web app.

Agent roles are exactly **Orchestrator, Planner, Safety, Executor, Verifier**. Research, implementation, workspace operations and bounded JSON testing belong to Executor; independent verification and review belong to Verifier. Windows uses handle-relative local NTFS operations with reparse-point rejection and owner-only file permissions. macOS and Linux use POSIX descriptor-relative no-follow operations. The three-OS test matrix is in `.github/workflows/cross-platform.yml`; unsupported filesystem configurations fail closed.

Foundation for **Autonomous AI Agent for Intelligent Task Execution**. The React frontend now presents a responsive conversation workspace with Light, Dark and System themes, a collapsible sidebar, a centered new-task composer that docks at the bottom of saved conversations, compact suggestion chips, saved task conversations, readable AI plans, and expandable task activity. FastAPI continues to provide persistent tasks and real planning. Configuration, explicit CORS, consistent errors, and the original health endpoint are preserved. Tasks can now deliberately request a real OpenAI-generated plan. The existing planning action remains review-only. A separate opt-in, local-only orchestration API now coordinates Planner, Safety, Executor and Verifier roles for bounded workspace files. Authentication and public deployment remain out of scope.

## Setup

Use Node.js 22.12+ (Node 24 recommended) and Python 3.11+ (verified here with 3.14.7). Run from the project root:

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock.txt
[ -f .env ] || cp .env.example .env
cd ../frontend
npm ci
[ -f .env ] || cp .env.example .env
```

On Windows, activate with `.venv\Scripts\Activate.ps1`. No OpenAI key is required for startup, task management, or tests. Real planning requires a backend key and available OpenAI API quota. Phase 3 dependency installation succeeded; package-lock.json and requirements-dev.lock.txt preserve the tested versions. The Python lock records the Python 3.14/macOS environment; verify wheel availability on other platforms.

## Run

Backend, in terminal 1 from the project root:

```sh
cd backend
source .venv/bin/activate
python -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Frontend, in terminal 2 from the project root:

```sh
cd frontend
npm run dev
```

Open http://127.0.0.1:5173. API documentation: http://127.0.0.1:8000/docs.

```sh
curl --fail http://127.0.0.1:8000/api/v1/health
```

Expected response:

```json
{"status":"ok","service":"agentx-api","database":"ok","phase":1}
```

The page calls the real backend, reports errors, times out after five seconds, and supports retry. It contains no simulated AI output.

## Configuration and security

Backend reads `backend/.env` independently of the working directory. Process environment overrides the file. Relative DATABASE_PATH values resolve under backend/. Database parent directories and schema_version are created during startup; repeated initialization is safe. Connections use transactions, foreign-key enforcement, and explicit close. Schema migration 2 adds tasks and task_history in a single transaction, preserving version 1 and existing data. See the task API section below.

| Variable | Location | Default / meaning |
| --- | --- | --- |
| OPENAI_API_KEY | Backend only | Empty by default; required only for real planning |
| OPENAI_MODEL | Backend only | gpt-4.1-mini; must support Responses structured outputs |
| OPENAI_TIMEOUT_SECONDS | Backend only | 45; accepted range 5–60 seconds |
| CORS_ORIGINS | Backend only | JSON array containing http://localhost:5173 and http://127.0.0.1:5173 |
| DATABASE_PATH | Backend only | data/agentx.sqlite3 |
| VITE_API_BASE_URL | Frontend, public | http://localhost:8000 |

CORS allows only explicit HTTP(S) origins with no paths or wildcards, GET and POST requests, and Content-Type. No additional origins or credentials are enabled. CORS is not authentication. Never put secrets in VITE_ variables: they become public build content. Environment files, databases, dependencies, and build outputs are ignored by Git. This empty workspace is not yet a Git repository; no commit or remote push was made.

Invalid configuration and database initialization errors fail startup with actionable messages. Missing OpenAI configuration does not prevent startup; the planning endpoint returns a safe 503 without starting an attempt. There is no shell execution, arbitrary-code execution, or user-supplied executable code path.

## API conventions

- Existing health route: `/api/v1/health`. Phase 2 task routes use the requested `/api/tasks` prefix. JSON request/response bodies; snake_case keys.
- Success: typed Pydantic response object with HTTP 2xx.
- Failure: `{"error":{"code":"machine_readable_code","message":"Safe human-readable message."}}`.
- Invalid requests: 422; unknown routes: 404; unavailable database: 503; unexpected failures: 500.
- Incoming models inherit APIModel, which rejects extra fields. Health has no input body. Task creation validates a strict string description, rejects blank or over-5,000-character input, and trims surrounding whitespace.
- Errors do not echo input values or stack traces. OpenAI secrets use Pydantic SecretStr.
- Frontend checks health, task, list, and history responses at runtime as well as using TypeScript types.

## Tests and build

From the project root, after setup:

```sh
cd backend
source .venv/bin/activate
python -m pytest -q
PYTHONPATH=. python tests/smoke_check.py
cd ../frontend
npm test
npm run typecheck
npm run build
```

`npm run preview` serves the production build locally; it is not a production host.

## Dependencies

Backend pins: FastAPI 0.141.1, Uvicorn 0.52.3, Pydantic 2.13.4, pydantic-settings 2.15.0; test dependencies httpx 0.28.1 and pytest >=8,<10. OpenAI SDK 3.16.2 provides the Responses API integration. SQLite uses Python's standard library. The full tested development dependency set is pinned in requirements-dev.lock.txt.

Frontend pins: React and React DOM 19.3.0, Framer Motion 13.2.0, Vite 8.3.3, TypeScript 6.0.3, @vitejs/plugin-react 6.1.2, @types/react and @types/react-dom 19.3.0. Test ranges: Vitest ^4.0.0, jsdom ^27.0.0, Testing Library React ^16.3.0 and jest-dom ^6.9.1. Phase 3 installed these dependencies and verified the frontend tests, TypeScript and production build. Resolved versions are pinned in package-lock.json.

LangGraph is not installed. The orchestration foundation uses an explicit SQLite-backed state machine and typed agent interfaces without adding dependencies.

## Deployment preparation (not deployed)

Build frontend with `npm run build` and publish frontend/dist to a static host. Set VITE_API_BASE_URL to the HTTPS backend origin before building. Backend command: `python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000` (adapt port to the host). Set CORS_ORIGINS to the exact public frontend origin and DATABASE_PATH to a persistent writable volume. Use one backend instance for the initial SQLite design. Configure TLS and secrets at the host. Keep EXECUTION_ENABLED=false on hosted instances. Execution is local-only and must not be exposed through a reverse proxy; authentication and user isolation are not implemented.

## Task API and schemas

| Method and route | Behavior |
| --- | --- |
| POST /api/tasks | Accepts `{"description":"Your task"}`; returns Task with 201 Created |
| GET /api/tasks?limit=20&offset=0 | Returns `{tasks: Task[], limit, offset}`, newest first; limit 1–100, offset 0–100000 |
| GET /api/tasks/{task_id} | Returns Task; UUID required; 404 if absent |
| GET /api/tasks/{task_id}/history | Returns `{events: HistoryEvent[]}` in timestamp/ID ascending order; 404 if task absent |

Task fields: `id` (UUID), `description`, `status`, `created_at`, `updated_at` (UTC ISO timestamps), `plan` (nullable JSON object), `final_result` (nullable string), and `error` (nullable safe display string). Creation sets status to pending and plan/result/error to null. Timestamps are equal at creation.

HistoryEvent fields: `id` (integer), `task_id`, `event_type` (task_created or status_changed), `status`, `message`, and `created_at`. Creation writes the task and initial history event atomically. No client may set status, plan, result, or errors through the creation endpoint.

Declared states: pending, planning, awaiting_approval, running, completed, failed, cancelled. Creation still produces only pending. Planning adds the transitions described below. An internal, transaction-protected pending-to-cancelled persistence operation records a real cancellation event; it is not exposed in the API or UI. Orchestration maps executing and verifying to the existing running task status; the execution API exposes the precise phase. Planning transitions are handled separately with attempt ownership.

Error responses keep the existing `{error: {code, message}}` envelope. Invalid descriptions, extra fields, malformed UUIDs or pagination return 422. Missing tasks return 404. SQLite failures return safe 503 responses without SQL, paths, or underlying database messages. Reads and writes use parameterized SQL.

```sh
curl --fail-with-body -X POST http://127.0.0.1:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"description":"Organize my study goals"}'
curl --fail 'http://127.0.0.1:8000/api/tasks?limit=20&offset=0'
# Replace TASK_UUID with an ID returned above:
curl --fail http://127.0.0.1:8000/api/tasks/TASK_UUID
curl --fail http://127.0.0.1:8000/api/tasks/TASK_UUID/history
```

## Conversational interface

The frontend uses a theme-aware conversation layout with a collapsible desktop sidebar and an accessible mobile navigation drawer. Chat/New Task, Task History, Tools, Approvals and Settings share the same workspace. The history view searches the latest 20 real tasks returned by the existing API. Suggestions only fill the composer; they never create tasks or plan responses.

The auto-growing composer supports Enter to submit, Shift+Enter for a newline, IME composition, a 5,000-character limit, loading/disabled states, and input preservation when creation fails. A message creates one new persisted task. The current backend does not support follow-up messages within a task, so the composer explicitly identifies subsequent submissions as new tasks. No unsupported attachment or stop controls are shown.

Task descriptions appear as user messages. Generate AI Plan deliberately invokes the existing planning endpoint. Actual backend status is polled; no fabricated response, progress percentage, execution result or activity is added. Real plans render as assistant responses with numbered steps, dependencies, expected outputs, assumptions, constraints, and clarification questions. Task activity expands to show persisted timestamps, status and history events.

Markdown uses react-markdown 10.1.0 and remark-gfm 4.0.1, with semantic headings, lists, fenced code, links and tables. Raw HTML is skipped, unsafe link schemes use the renderer's default filtering, and images render as alt text rather than loading untrusted remote resources. Plan formatting is loaded on demand. Copy plan uses the real clipboard API and reports clipboard failures.

Tools and Approvals show explicit unavailable states. Settings offers an Appearance preference and displays the real API origin and health result; AI credentials remain backend-only. Model configuration and unsupported actions are not presented as editable controls.

The frontend checks health on startup and allows connection retry. Failed submissions retain the draft; failed task reads and planning operations have explicit retry/reload actions. A timeout or lost response after POST may mean a task was already saved: reload before resubmitting. There is no backend cancellation endpoint or task-creation idempotency protocol.

See CONVERSATIONAL_UI_REPORT.md for this milestone's changed files, final test/build results and browser verification. Historical phase reports are retained. Startup and test commands below are unchanged; run `npm ci` after pulling the updated lockfile.

## Appearance preferences

Choose **Light, Dark or System** in Settings → Appearance, or use the compact header selector. Light uses a white background, black text and black primary buttons with white labels. Dark uses a black background, white text and white primary buttons with black labels. Shared neutral surfaces, borders and focus styles apply throughout the workspace. Multiple accent presets and custom colors have been removed.

The appearance preference persists locally as `agentx.theme` and synchronizes between same-origin tabs. System is the default and follows OS changes immediately. Invalid values fall back to System. Retired `agentx.accent` preferences are ignored before paint and removed when the application loads. If storage is blocked, the selected mode still applies for the session and Settings explains the persistence failure. Reset to default restores System.

All colors are shared semantic tokens in `frontend/public/themes.css`. Synchronous `theme-init.js` resolves the appearance before React starts to reduce incorrect-theme flashes. No color engine or extra dependencies are needed. Semantic error/warning indicators retain their meaning.

See MONOCHROME_THEME_REPORT.md for this refinement's changed files, verification and limitations. Earlier theme reports describe historical milestones. Backend and frontend run commands remain unchanged.

## Schema upgrade

Startup uses `BEGIN IMMEDIATE` to serialize migration and wraps tables, indexes, and the version marker in one transaction. Existing schema_version row 1 is retained; row 2 is inserted only after successful migration. `tasks` uses a UUID primary key, constrained status and description, timestamps and nullable output fields. `task_history` has a foreign key to tasks, monotonic integer event IDs, and constrained event/status fields. Indexes support recent tasks and per-task chronological history. Repeated startup does not recreate or erase stored tasks. Future schemas need subsequent migrations; existing schema-version markers must not be edited manually.

## Real AI planning

Set backend/.env (never frontend/.env) with your own OPENAI_API_KEY. Keep this file untracked. Optional settings:

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
OPENAI_TIMEOUT_SECONDS=45
```

The empty key above is a placeholder, not a working credential. Restart the backend after editing configuration. The frontend needs only VITE_API_BASE_URL.

`POST /api/tasks/{task_id}/plan` has no request body. It returns the saved Task with HTTP 200 after a validated plan is persisted. The normal sequence is pending -> planning -> awaiting_approval. The final state means **plan available for human review**; this endpoint does not start the separate execution workflow. final_result remains null. The frontend offers Generate AI Plan, polls the stored status while waiting, and reloads task/history after the request.

The backend uses the official OpenAI Python SDK, Responses API `responses.parse(text_format=TaskPlan)`, and strict Pydantic validation before saving. The request uses the configured model, a 6,000-output-token cap, store=false, no tools, zero SDK retries, and both SDK and overall response deadlines. The API origin is fixed to https://api.openai.com/v1. Provider request/response debug logging is suppressed. Neither key nor provider error payloads are logged or returned.

Plan fields:

- goal: non-blank interpretation, at most 1,000 characters.
- assumptions and constraints: up to 10 non-blank strings each, at most 300 characters each.
- steps: 1–12 consecutive steps. Each has number, action (at most 300 characters), purpose and expected_output (at most 1,000 each), and dependencies (unique earlier step numbers only).
- clarification_needed: strict boolean; must agree with whether clarification_questions contains questions.
- clarification_questions: up to 10 non-blank questions, at most 300 characters each.

Extra fields, invalid types, cyclic/forward dependencies, missing fields, refusals, incomplete responses and invalid outputs are rejected. Output is untrusted text rendered by React; it is never interpreted as code, HTML or tool instructions. Structural validation does not guarantee factual correctness: review every proposed plan.

| Outcome | HTTP / saved behavior |
| --- | --- |
| Valid plan | 200; plan and awaiting_approval history event saved together |
| Unknown/malformed task ID | 404 / 422; no AI request |
| Ineligible state or duplicate in-flight request | 409; no additional AI request |
| Missing key | 503 ai_not_configured; preflight leaves existing state untouched |
| Rate limit/quota | 429; safe failed state and history |
| Timeout | 504; safe failed state and history |
| Connection/authentication failure | 503; safe failed state and history |
| Provider rejection, refusal or invalid output | 502; safe failed state and history |
| Storage failure | 503; transaction rolls back, failure recorded if storage allows; expiry recovery otherwise |

Retry is explicit: POST the same endpoint again. Eligibility extends pending to **failed tasks whose planning_attempts record is failed and whose plan is null**. Unrelated failed tasks, active planning, saved plans, cancelled/running/completed tasks cannot plan. This narrow extension supports retries without a new task or an arbitrary status mutation endpoint. Each retry generates a new attempt ID and may incur a new provider charge. No automatic provider retry or fabricated fallback exists.

```sh
# Substitute an ID obtained from POST /api/tasks:
curl --fail-with-body -X POST http://127.0.0.1:8000/api/tasks/TASK_UUID/plan
```

## Planning ownership and recovery

Migration 3 adds planning_attempts, keyed by task ID, with token, expires_at and outcome (active/failed/succeeded). Existing tasks/history/version rows are preserved. BEGIN IMMEDIATE serializes claims. A claim atomically changes the task to planning, stores ownership, and writes its history event. The write transaction closes **before** OpenAI is contacted. Completion checks status, token, active outcome and expiry, then atomically saves the plan or failure and history. A late/stale response cannot overwrite another attempt.

An attempt expires after OPENAI_TIMEOUT_SECONDS + 30 seconds (75 seconds by default). Startup and a five-second recovery loop mark expired/orphaned planning tasks failed with a safe history event. The loop retries if SQLite is temporarily unavailable. Unexpired attempts are left alone on restart to avoid cancelling work in another process. For a local one-process installation, a crash can therefore leave planning visible until expiry; no process means no recovery until restart. Persistent database failure can delay recovery indefinitely. No heartbeat, queue or distributed worker is implemented.

Client timeouts/disconnections and process failures do not guarantee that OpenAI cancelled its work; a provider charge may occur even when no plan was persisted. Reload before retrying. Frontend planning requests wait up to 75 seconds (the backend request deadline is capped at 60); other task calls retain their 10-second timeout. Polling reads saved state and never invents progress percentages.

## Verification and phase boundary

See PHASE3_REPORT.md for actual results. PHASE1_REPORT.md and PHASE2_REPORT.md remain historical records, including earlier environment blockers. Phase 3 successfully installed dependencies and passed the full suites, TypeScript and production build. Live local HTTP and browser missing-key flows were verified. The SDK parser was exercised with a mock HTTP transport; **no live OpenAI generation was performed**.

The historical Phase 3 milestone did not include execution. The new orchestration foundation below adds bounded tools and approval; authentication, user isolation, LangGraph and deployment remain out of scope. Keep this unprotected local prototype off public networks. Plans and user descriptions are persisted in local SQLite; generating a plan transmits its description to OpenAI. No changes were made to CORS origins or credential policy.

Implementation references: [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini), [official Python SDK](https://github.com/openai/openai-python).


## Orchestration foundation — Phase 1

The existing frontend layout, theme preferences and `/plan` contract are preserved. Starting execution and approving plans remain API-only. The UI polls selected running tasks, displays the saved verified result and refreshes Task Activity when execution stops. It does not yet offer execution/approval controls; Tools/Approvals placeholders remain unchanged.

Four typed responsibilities are coordinated by `app.execution.orchestrator.Orchestrator`:

- **Planner:** real backend OpenAI structured output (`ExecutionPlan`), using the existing model, timeouts and error handling. No fake fallback.
- **Safety:** deterministic policy validates paths, dependencies, supported tools, credential patterns and approval requirements. Model assurances cannot bypass it.
- **Executor:** runs actual `read_text`, exclusive new-file `write_text`, and `validate_json` workspace operations. Dependencies execute in order; only explicitly transient read-only failures receive bounded retries.
- **Verifier:** independently rereads actual files, checks SHA-256, byte counts and exact approved written content, and reruns JSON parsing/required-key tests. Completion means these concrete postconditions passed; it does not certify arbitrary semantic goals or execute a project's test suite.

Only after separate authorization, provision a dedicated workspace directory and enable locally in backend configuration, then restart the backend. The workspace root and intermediate directories must already exist. Startup opens the root read-only and never creates directories; a missing root fails closed. Preparing/reviewing a plan, missing approval and rejection never create workspace files or directories:

```dotenv
EXECUTION_ENABLED=true
WORKSPACE_PATH=workspace
EXECUTION_MAX_ATTEMPTS=2
EXECUTION_LEASE_SECONDS=120
```

Defaults: execution disabled, dedicated `backend/workspace`, two attempts (range 1–3), 120-second execution lease (range 30–300). Relative workspace paths resolve under backend. Keep the database, source, configuration and credentials outside this directory. Files must be ordinary UTF-8 `.txt`, `.md`, `.json` or `.csv`, no larger than 64 KiB; each planned write is at most 8,000 characters. Intermediate directories must already exist. Hidden paths, traversal, symlinks, hardlinks, special files and known credential filenames are rejected. Windows uses directory-handle-relative NTFS opens that reject reparse points; macOS and Linux retain POSIX descriptor-relative no-follow operations. UNC paths and unsupported filesystems fail closed.

Every execution plan, including read-only plans, requires explicit approval of its exact saved plan digest, which also binds workspace identity. Existing files are never overwritten. Destruction, shell/code execution, network actions, email and other external side effects are unsupported and cannot be enabled by approval. Unsupported or incomplete plans fail safely and require a new, clarified task. No tool output is sent back to the model. Known credential patterns and the configured API key are blocked in planning input, generated plans and workspace output. This is a conservative filter, not a universal secret detector: the workspace must contain only data intended for the local user.

| Method and route | Behavior |
| --- | --- |
| POST `/api/tasks/{task_id}/orchestrate` | Pending task only. Plans, validates and persists steps plus the approval digest; always pauses at awaiting_approval without executing tools. Returns Execution with 200. |
| GET `/api/tasks/{task_id}/execution` | Returns durable phase, exact tool calls, dependencies, attempt counts, results, verification evidence and safe errors. |
| POST `/api/tasks/{task_id}/approval` | Body `{ "plan_digest": "64-character digest from execution", "decision": "approve" }` or `reject`. Matching approval executes; rejection cancels without executing. |

Execution fields: `run_id`, `task_id`, `phase`, `plan_digest`, `approval_required`, `approval_decision`, `steps`, `error`. Each step includes `number`, `dependencies`, `tool`, `status`, `attempts`, `result`, `verification`, `error`. Tool fields are `name`, `path`, nullable `content`, and `required_keys`. Review all tool calls and their exact write content before approval. Results contain actual byte count, hash, read content where applicable and JSON check identifiers; verification records independent check evidence.

Lifecycle: pending → planning → awaiting_approval (all plans) → executing → verifying → completed, or failed. Rejection becomes cancelled. Existing Task status is `running` during executing/verifying for frontend compatibility. Task history keeps its existing event schema and includes agent-prefixed messages, attempts and verification outcomes. Final results are saved only after all verification passes. Tool failure can return a 200 Execution with phase `failed`; inspect the phase, not just the HTTP code.

New API errors use the existing envelope: 422 invalid input/unsafe plan/clarification; 404 missing task/run; 409 duplicate, stale approval or ineligible state; 503 disabled execution, missing key or unavailable storage. Provider errors retain existing 429/502/503/504 mappings. Execution endpoints require a loopback client, localhost Host and an allowed Origin when present, and reject cross-site fetch requests and browser navigation/no-CORS modes. Internal approval and every tool authorization also recheck the execution setting and saved task state; tool authorization requires the saved approval-required flag as well as matching approval evidence. These controls are not authentication; bind to 127.0.0.1 and do not forward execution endpoints through a proxy.

Example (replace TASK_UUID and REVIEWED_PLAN_DIGEST after reading responses):

```sh
curl --fail-with-body http://127.0.0.1:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"description":"Create answer.json with the JSON object {\"answer\":42}, then validate that answer is present."}'
curl --fail-with-body -X POST http://127.0.0.1:8000/api/tasks/TASK_UUID/orchestrate
curl --fail http://127.0.0.1:8000/api/tasks/TASK_UUID/execution
# Review the exact tool calls and content before this explicit approval:
curl --fail-with-body http://127.0.0.1:8000/api/tasks/TASK_UUID/approval \
  -H 'Content-Type: application/json' \
  -d '{"plan_digest":"REVIEWED_PLAN_DIGEST","decision":"approve"}'
curl --fail http://127.0.0.1:8000/api/tasks/TASK_UUID/history
```

Migration 4 atomically adds `execution_runs`, `execution_steps` and `execution_approvals`, plus an expiry index. Existing tasks, plans and history remain intact. Run ownership, transitions and history are transaction-protected; no SQLite write transaction spans an OpenAI request. An explicit allowed-transition map rejects skipped/backward transitions; completion also requires persisted verification for every step. Executor dispatch uses an immutable tool registry after schema validation. Bounded local tool operations are fenced by short write transactions. Approvals are rechecked before every tool call, including read-only calls. Planning cannot transition directly to executing. Final independent checks and the completed commit share a transaction.

A startup/five-second recovery loop marks expired execution failed without replay. Unexpired work is left alone until its lease expires. A process crash or storage failure after a write may leave a partial or complete file without a committed result. SQLite and the filesystem cannot commit atomically; inspect the workspace and create a new task rather than replaying mutations. Only one orchestration run per task is supported, and `/plan` cannot replace that run. Other trusted local processes can still change workspace files; there is no OS sandbox or distributed queue. Task completion reflects outputs at verification time.

See `ORCHESTRATION_PHASE1_REPORT.md` for changed files, actual verification results and remaining limitations. No dependencies, CSS or theme files were changed. Existing planning components received targeted status polling and verified-result display changes.

## Five-agent JSON evidence demo

The optional `ExecutionPlan.workflow = "five_agent"` adds a bounded, real local-data workflow. It requires exactly three approved tool steps: `read_text` an existing JSON source, `write_text` a different new destination depending on step 1, then `validate_json` that destination with required keys depending on step 2. Existing workspace plans and their digests remain compatible.

Exactly five roles run for both ordinary workspace and JSON evidence workflows:

- **Orchestrator — Team Leader:** coordinates lifecycle, dependencies, bounded retries and the combined verified report.
- **Planner — Thinker:** makes the real OpenAI structured-output call. Planning runs zero tools.
- **Safety — Security Guard:** validates policy, paths and exact-plan approval; rechecks authorization before every tool.
- **Executor — Doer:** runs only approved tools. Local-file research, evidence-derived JSON creation and bounded JSON test rechecks are capabilities, not additional agents. Writes are never retried.
- **Verifier — Quality Checker:** independently rereads files, compares content/hashes/size, reruns JSON tests and checks completion evidence and safety policy.

The existing `five_agent` workflow value remains compatible with saved plans and digests; it selects the stricter three-step JSON capability contract, not a different agent roster. No database migration is needed: progress is reconstructed from persisted lifecycle, approvals, results and verification records, including historical runs. Testing is a capability, not a sixth agent. In the workspace execution workflow, only Planner makes model calls; the other roles use deterministic code and permitted local tools. The direct-answer chat workflow below uses a second model call in Executor.

`GET /api/tasks/{id}/execution` adds structured `agents` progress derived from persisted plan, steps, attempts, results and verification history. Failed or unrun roles are shown as failed/skipped, never successful. In the existing task conversation, **Show agent activity** displays live status, input/output summaries, errors, hashes, contents and check evidence. This UI is read-only; prepare and approval actions remain API-only.

For a live demo, first obtain explicit permission to create `backend/workspace/demo-source.json` containing exactly `{"answer":42}`, or use an existing user-approved source. Obtain permission separately to run the backend with execution enabled on `127.0.0.1`. Then create a fresh pending task with a unique destination filename and this description:

> Prepare a five-agent JSON workflow. Research existing demo-source.json; implement a NEW demo-output-UNIQUE.json containing exactly {"answer":42}; test the top-level key answer; independently review all evidence. No tools may run before my separate approval of the exact saved plan.

Use the existing POST `/orchestrate`, confirm all five role entries and inspect the exact tools/content/dependencies plus `plan_digest` in GET `/execution`, and stop for human review. Only a separate explicit POST `/approval` with the matching digest starts tools. The standard `/plan` button remains proposal-only and cannot start this execution workflow. No live five-agent success is implied by automated tests; they mock model/network responses and use real isolated temporary files and SQLite databases.


## Persistent conversation management

The sidebar provides New Chat, server-side search, paginated active/archived history, rename, archive/restore and confirmed deletion. One conversation corresponds to one existing persisted task; this does not add a multi-turn messaging engine. Selecting a task reloads its details, plan, history and read-only agent activity from the backend.

Database migration 5 adds nullable `title` and `archived` (default false) without changing existing task plans or digests. Existing list behavior is preserved when no filters are provided:

- `GET /api/tasks?limit=20&offset=0&q=term&archived=false`: literal search over title, request, saved plan, result, error and history messages. Omit `archived` for both groups.
- `POST /api/tasks/{id}/conversation`: JSON `{ "title": "My chat" }` and/or `{ "archived": true }`; false restores. Metadata does not alter execution ownership, approval digests or task status.
- `DELETE /api/tasks/{id}`: permanently removes the task and all related database records in one transaction. Planning/running tasks are rejected. Workspace files are retained; the UI confirmation explains this.
- `GET /api/tasks/{id}/activity`: read-only persisted evidence, available with execution disabled. Execution runs reuse existing progress reconstruction. Proposal-only `/plan` tasks show exactly five roles, with non-invoked roles skipped/pending and no executable digest. For proposal-only tasks, only Planner calls OpenAI.

Rename/archive/delete require a trusted loopback connection and allowed origin. The existing execution endpoints and approval gate remain unchanged. An already-running backend must load updated code/migration before the new endpoints are available; editing source alone does not update its process. Do not restart or enable execution without authorization. Frontend data always comes from database APIs; no local-only conversation persistence or fabricated results are used.


New task submission now requests `/plan` automatically once saved details load; Enter and Send use the same path. Reopening an existing task does not trigger a model call. Failed planning requires a deliberate retry. Responses remain structured proposals; no orchestration, approval or workspace tool is invoked automatically. Sending a task shares its description with OpenAI, as stated beside the composer.


## Direct answers and code (default chat experience)

New messages call `POST /api/tasks/{id}/respond` automatically. The answer is natural Markdown, including copyable fenced code blocks; internal plan cards are not the default response. Existing `/plan`, `/orchestrate`, execution digests and explicit workspace approval remain compatible.

The text-only five-role workflow is real and persisted: Orchestrator claims/coordinates the task; Planner calls OpenAI for an internal TaskPlan; Safety validates the text-only schema and secret policy; Executor makes a second OpenAI call for the actual answer/code; Verifier checks nonempty text, secrets, balanced code fences and a SHA-256 fingerprint; Orchestrator saves the final answer. These checks do not establish factual accuracy or that generated code runs. Exactly two model calls per successful direct response, not five independent LLMs.

`GET /activity` reads durable role results from `chat_runs`; final Markdown is in `tasks.final_result` and the additive response field `response_mode=chat` identifies it. Migration 6 adds only a separate `chat_runs` table so older task readers remain compatible. Delete removes chat evidence transactionally along with the existing task/history records. Active responses cannot be deleted; expired requests fail through existing lease recovery. Failures retain real role states; no automatic model retries or tool execution occur.

This workflow works with `EXECUTION_ENABLED=false`: it generates text, never initializes a Workspace, executes code, writes workspace files or approves an execution plan. File/tool operations still require a separately saved execution plan and explicit matching approval.

## Phase 2 sandbox implementation status — live activation blocked

The safety-only deadline follow-up is documented in [backend/DEADLINE_DESIGN.md](backend/DEADLINE_DESIGN.md).
It adds non-renewable native-lease contracts, exact-container cleanup, restart
reconciliation and independently retrieved deadline receipts. The required
platform-native cutoff backend is not available or provisioned. Docker START is
delegated to that authority; there is no supervisor-local Timer fallback, and
the unconditional live-activation block remains in force.

Both `EXECUTION_ENABLED` and `PYTHON_EXECUTION_ENABLED` default to false. No Docker installation, image pull/build, VM/container start or live Python validation was performed for this phase. The new path is tested only with fake runtimes, opaque source strings and disposable databases. Existing file-plan digest serialization is preserved.

An explicit `workflow=python` query on the existing orchestration endpoint selects one standalone `run_python_function` step. The Planner proposes a function and declarative JSON cases; the server computes exact source/test hashes and supplies operator-pinned runtime policy. Preparation never contacts a runtime. Existing chat messages still use text-only `/respond`. There are exactly five roles; testing remains a Verifier capability, not another agent.

Python plans bind source, cases, immutable image ID, bootstrap/profile hashes, daemon identity, permissions and fixed limits into the existing approval digest. The limits are 1 CPU, 256 MiB RAM with no additional swap, 16 processes, 16 MiB tmpfs, 64 KiB combined job output, five seconds per case, thirty seconds per job, ten cases and one active job. No host mounts, inherited backend environment, network, privileged user, shell/CLI, dependency installation or host evaluation is offered. The container root is read-only and seccomp denies syscalls by default. These are configured requirements, not independently proven isolation guarantees.

`execution/sandbox.py` is the Unix-socket client; `docker_supervisor.py` contains the Docker API adapter and durable supervisor ledger; `supervisor_service.py` is a separately operated service definition. Neither service nor image is provisioned or started by AgentX. The service independently reads the backend's exact approval, plan, step and ownership records before and during a case. The container-only `sandbox_image/runner.py` must never be imported or executed by backend/tests. It would be packaged into a separately reviewed immutable image; no image currently exists as part of this implementation.

Migration 7 adds a `python_jobs` intent/evidence ledger. Runtime I/O and independent receipt retrieval occur outside SQLite write transactions. Actual exits, stdout/stderr, case observations, hashes, runtime config and cleanup evidence have typed records. The Verifier compares a separately fetched supervisor receipt and approved JSON expectations; printed PASS messages are not accepted. Unknown cleanup quarantines new Python work and blocks deletion of the affected conversation. Recovery only reconciles/stops recorded jobs; it never resubmits source. Supervisor receipts persist independently from conversation deletion.

**Safety blocker:** a supervisor-local watchdog survives backend failure but cannot enforce a wall-clock deadline if the supervisor itself crashes. `DockerRuntime.preflight()` therefore unconditionally refuses live activation, before Docker API calls. Neither feature flags nor a manifest can override this release block. The remaining adapter implementation is disabled scaffolding. A separately reviewed crash-independent deadline mechanism is required before any activation work; this is not a completed operational sandbox.

Separate approval is required for the deadline design change, Docker/VM/image provisioning, image/profile audit, and each exact live test/environment (correct/incorrect function cases, network/host/environment probes, CPU/memory/process/output limits, backend and supervisor interruption, cleanup and recovery). Do not enable the flags or send a real approval based on mocked test results. No authentication/administrator-resistant isolation is claimed.

When an older backend process cannot be stopped by this session, the updated demo can run separately without changing `.env`:

```sh
# backend directory
EXECUTION_ENABLED=false CORS_ORIGINS='["http://127.0.0.1:5174","http://localhost:5174"]' .venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001
# frontend directory, separate terminal
VITE_API_BASE_URL=http://127.0.0.1:8001 npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

Use http://127.0.0.1:5174 for that process-only demo. The original backend and its frontend retain their original ports. This is one request per saved conversation/task; multi-turn conversation memory and code execution sandboxes are not implemented.
