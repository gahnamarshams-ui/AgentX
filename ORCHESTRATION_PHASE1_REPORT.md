# AgentX orchestration foundation — Phase 1

Verified 2026-10-20. Implementation continued from existing files after the interrupted response. The existing layout, themes, dependencies, authentication and task/planning routes were preserved. Small frontend status updates now poll running tasks and display saved verified results. This directory has no Git metadata, so `git diff --stat` could not run; changes were inspected directly. No commit or push was made.

## Architecture and implemented behavior

The existing FastAPI/Pydantic backend, SQLite task/history database, OpenAI SDK configuration and transactional planning ownership are reused. A central Orchestrator enforces a deterministic allowed-transition map, requires recorded approval before sensitive execution and verified step evidence before completion, and coordinates four typed, injectable interfaces: an OpenAI Planner, deterministic Safety policy, real workspace Executor, and independent output Verifier. Only the Planner uses a model; the other agents perform actual policy, I/O and verification work.

New migration 4 adds execution_runs, execution_steps and execution_approvals without replacing existing tables. Runs persist exact plans, ownership and expiry; steps persist dependencies, attempts, actual outputs and verification; approvals bind the exact plan and workspace identity. Existing task status remains `running` during executing/verifying; the execution endpoint exposes those distinct phases. Task activity records each role's actual actions using the existing history contract.

Executor dispatch uses an explicit immutable registry and revalidates tool-call schemas at the execution boundary. Supported tools: read bounded UTF-8 text, create a new file exclusively, and validate JSON plus required top-level keys. Every write requires explicit approval. Existing files cannot be overwritten. Shell execution, arbitrary code, deletion, networking and other external side effects are unsupported. Workspace access rejects traversal, hidden paths, symlinks, hardlinks, special files and unsupported extensions. Secret checks cover the configured API key and common credential patterns; errors do not echo provider or filesystem payloads.

Verifier checks real file bytes, SHA-256, byte counts, exact approved content and independently repeated JSON checks. Task completion is committed only after every step passes these postconditions. This does not claim semantic correctness for arbitrary goals or run repository test suites as an agent tool.

Duplicate planning and approvals are rejected. Only explicitly transient read-only errors retry (default two attempts, maximum three). Mutations never retry automatically. Expired execution fails without replay. SQLite transactions do not span model requests; bounded filesystem work and final verification are fenced by short write transactions.

## Changed files

New:

- backend/app/execution/__init__.py — execution package.
- backend/app/execution/models.py — bounded plans, tool calls, approval and execution responses.
- backend/app/execution/security.py — safe errors and credential filtering.
- backend/app/execution/workspace.py — restricted descriptor-relative filesystem access.
- backend/app/execution/agents.py — four interfaces and their real implementations.
- backend/app/execution/store.py — durable execution snapshots, integrity checks and recovery.
- backend/app/execution/orchestrator.py — lifecycle, approval, dependencies, retries and verification.
- backend/app/execution/routes.py — opt-in local execution endpoints.
- backend/tests/test_execution.py — 58 additional unit/integration test cases.
- ORCHESTRATION_PHASE1_REPORT.md — this report.

Updated:

- backend/app/config.py — execution opt-in, dedicated workspace and bounded retry/lease settings.
- backend/app/database.py — transactional, idempotent migration 4.
- backend/app/ai.py — reuse structured-output integration for executable and review-only plans.
- backend/app/planning.py — shared recovery and prevent legacy planning from replacing orchestration runs.
- backend/app/main.py — execution startup, routing and safe error responses.
- backend/tests/test_tasks.py — migration expectation includes version 4.
- backend/tests/smoke_check.py — fixes the supplementary migration expectation to version 4.
- frontend/src/PlanPanel.tsx — polls running state, displays saved completion results and reloads activity when execution stops.
- frontend/src/PlanResponse.tsx — status-aware plan wording avoids false claims that executed steps never ran.
- frontend/src/PlanPanel.test.tsx — covers running-to-completed display and polling failure.
- backend/.env.example — placeholders and disabled-by-default execution settings.
- .gitignore — exclude workspace outputs.
- README.md — APIs, configuration, local commands and limitations.

Frontend layout, CSS, themes and dependency lockfiles were unchanged. Three planning component/test files received targeted status changes. The production build regenerated frontend/dist.

## API

- POST /api/tasks/{task_id}/orchestrate: pending task only; real structured planning, then await approval for writes or execute read-only steps. Returns an Execution object.
- GET /api/tasks/{task_id}/execution: exact planned tool arguments, phase, dependencies, attempts, outputs, verification and safe errors.
- POST /api/tasks/{task_id}/approval: `{ "plan_digest": "<reviewed digest>", "decision": "approve" }` or `reject`. Approval executes the exact plan; rejection cancels without tool execution.

Existing task/list/history/health and review-only `/plan` contracts remain. The frontend polls selected running tasks, displays saved verified results and refreshes activity at terminal state. Starting execution and approving plans remain API-only; Tools/Approvals placeholders are preserved.

Errors use the existing envelope. Unknown IDs return 404, invalid requests or unsafe plans 422, duplicates/stale approvals 409, disabled/missing configuration or database failure 503. Existing provider errors retain their safe mappings. A tool failure can return HTTP 200 with execution phase `failed`; clients must inspect the phase.

## Actual verification

Final verification after the latest resume and targeted fixes:

| Command/check | Result |
| --- | --- |
| `cd backend && .venv/bin/python -m pytest -q` | 122 passed in 1.65s; exit 0 |
| `cd backend && PYTHONPATH=. .venv/bin/python tests/smoke_check.py` | 10 passed in 0.211s; exit 0 |
| `cd frontend && npm test` | 47 passed, 6 test files; exit 0 |
| `cd frontend && npm run typecheck` | Passed; exit 0 |
| `cd frontend && npm run build` | Passed; 689 modules transformed; exit 0 |
| Isolated Uvicorn live loopback HTTP check | Health 200, task creation 201, missing-key orchestration 503, pending state retained, no execution run created |

Tests use real temporary SQLite databases/files and mocked SDK responses; no paid calls or real credentials are required. Coverage includes approvals, restart persistence, actual writes, JSON verification, fabricated outputs, duplicates/concurrency, retry exhaustion/success, blocked dependencies, invalid plans, missing keys, provider failures, secrets, filesystem restrictions, tampering, lease recovery and transaction failures after filesystem side effects. The original 64 tests also passed before changes. The latest resume added five backend cases for transition validation, approval/completion guards and tool registration, and two frontend cases. The supplementary smoke suite initially failed one stale schema-version assertion (expected 3 after migration 4); that assertion was fixed and all 10 checks then passed. No unresolved test failure remains.

The first live HTTP check was blocked by an inherited SOCKS proxy requiring unavailable socksio. It passed after disabling environment proxy use for the loopback-only test client (`trust_env=False`); no application dependency or network configuration was changed. npm emitted an existing non-fatal unknown http-proxy configuration warning.

No tests/builds remain running. Earlier development-server session handles were unavailable and HTTP probes confirmed ports 5173 and 8000 were not serving. Process inventory via pgrep was unavailable in this environment. No running user server was restarted. Live OpenAI generation, browser execution controls, Windows execution and deployment were not tested.

## Local run commands

From the project root, backend terminal:

```sh
cd backend
source .venv/bin/activate
EXECUTION_ENABLED=true python -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Use the existing backend-only OPENAI_API_KEY and OPENAI_MODEL configuration for real planning. Without a key the app starts and task APIs work, but planning returns a clear 503. WORKSPACE_PATH defaults to backend/workspace; keep it dedicated to approved task data. Execution remains disabled unless explicitly enabled.

Frontend terminal, from project root:

```sh
cd frontend
npm run dev -- --host 127.0.0.1
```

Open http://127.0.0.1:5173 and API docs at http://127.0.0.1:8000/docs. README contains creation, orchestration, review and explicit approval curl examples. Do not approve a digest without reviewing its tool calls and exact write content.

## Remaining limitations

- This is a local, API-first foundation. No frontend execution/approval controls, auth, user isolation, queue, distributed workers, external tools or public deployment.
- One orchestration run per task; failures require a new task. Clarification failures return a safe explanation, without an interactive clarification exchange.
- Execution uses POSIX file APIs. Local-only Host/client/Origin checks are not authentication and must not be placed behind a public proxy.
- File and database writes cannot commit atomically. A crash or database error can leave partial/complete output without a saved result; inspect the workspace before a new task. No automatic mutation replay or rollback is claimed.
- Recovery runs at startup and every five seconds, after lease expiry. Persistent database unavailability delays it. Client disconnect is not cancellation.
- The workspace is for trusted local data. Credential filtering cannot identify every possible secret. Other local processes can alter files; verification certifies bytes at verification time, not future immutability or an OS sandbox.
- Real OpenAI structured planning remains unverified against the live service in this milestone; SDK behavior is tested with mocks. No fallback plans or fake execution results were added.


## Latest resume inspection

Re-read existing orchestration source, Phase 1–3 reports and the prior orchestration report before editing. No Git repository exists, so no Git diff was available. Loopback probes confirmed the development servers were stopped. Previous tracked test/build commands had completed; the new verification commands above also exited successfully. No server was restarted and no existing task database or user workspace output was modified by testing.

The continuation changed only agents.py (explicit registration), store.py (transition guards), test_execution.py, smoke_check.py, PlanPanel.tsx, PlanResponse.tsx, PlanPanel.test.tsx, README.md and this report. It preserved the already completed orchestration APIs and migration instead of duplicating them. No live model execution, browser audit or deployment was performed during this continuation.
