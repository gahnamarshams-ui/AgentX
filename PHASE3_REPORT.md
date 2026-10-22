# AgentX Phase 3 — implementation and verification

Completed real backend OpenAI planning integration and persistence. No live OpenAI call was made; all provider success/error tests used mocks. No later-phase execution, tools, approvals, orchestration or deployment was added. Existing health/task routes and output envelope remain compatible.

## Changed files

| File | Purpose |
| --- | --- |
| backend/app/ai.py (new) | Official AsyncOpenAI structured parsing, backend key/model/deadline, bounded output, sanitized provider errors, suppressed SDK debug logging |
| backend/app/plan_schema.py (new) | Strict bounded plan/step validation and dependency/clarification consistency |
| backend/app/planning.py (new) | Atomic claim/finish, narrow retries, duplicate prevention, expiry recovery, POST planning route |
| backend/app/config.py | OPENAI_MODEL and bounded OPENAI_TIMEOUT_SECONDS |
| backend/app/database.py | Safe schema-v3 migration for planning_attempts; health expects schema 3 |
| backend/app/main.py | Planning router/error handler and lifecycle-owned recovery loop |
| backend/.env.example | Backend-only planning placeholders/settings |
| backend/requirements.txt | Pins official openai SDK 3.16.2 |
| backend/requirements-dev.lock.txt (new) | Actual installed direct/transitive development versions |
| backend/tests/test_planning.py (new) | Schema, SDK errors/parser, persistence, concurrency, recovery and rollback tests |
| backend/tests/test_tasks.py | Updates expected migration-version list to include 3 |
| backend/tests/smoke_check.py | Updates expected migration-version list to include 3 |
| frontend/src/api/client.ts | Typed plan model and runtime validator, planning call with 75-second client timeout |
| frontend/src/PlanPanel.tsx (new) | Real-status polling, generate/retry action, validated plan and clarification display |
| frontend/src/PlanPanel.test.tsx (new) | Plan rendering, backend status, success refresh, errors/retry and malformed-plan tests |
| frontend/src/TaskWorkspace.tsx | Integrates planning and refreshes task/history without discarding planning errors |
| frontend/src/App.tsx | Phase 3 wording; retains health checks |
| frontend/src/styles.css | Small plan/review additions to the existing design |
| frontend/index.html | Current page title |
| frontend/package-lock.json (new) | Actual resolved frontend dependencies; no declared versions changed |
| README.md | Current setup, schema/API/recovery behavior and limitations |
| PHASE3_REPORT.md (new) | This report |

No working architecture or files were deleted. Prior reports remain unchanged. No Git repository exists in this workspace, so no commit/push was performed.

## API and state behavior

POST /api/tasks/{UUID}/plan takes no body and returns the saved Task on 200. pending -> planning -> awaiting_approval; the latter means a plan is ready for review, not that approval was requested through a workflow or execution happened. final_result stays null.

Plan contains goal, assumptions, constraints, ordered steps (number, action, purpose, expected_output, dependencies), clarification_needed and clarification_questions. Limits and strict validation are documented in README.md. The SDK and persistence boundary both validate output before saving.

Missing-key preflight returns 503 without changing task state. Unknown/invalid IDs return 404/422. Ineligible or concurrent requests return 409. Provider limits return 429, timeouts 504, connection/auth errors 503, invalid/provider errors 502; these attempts become failed with safe history where storage permits. No failure becomes a fake plan. The only retry extension beyond pending is a failed planning attempt with no saved plan. No unrelated failed task is eligible.

Schema 3 adds planning_attempts (task_id primary/foreign key, token, expires_at, outcome) and an expiry index. Task/history tables are unchanged. Claims and completions use separate short transactions with no write lock held during OpenAI calls. A token/expiry check rejects stale completion. Migration and completion history writes are atomic.

Recovery runs on startup and every five seconds, marks expired/orphaned planning tasks failed, and records history. Default expiry is 75 seconds (45-second AI deadline + 30-second margin). Unexpired work is preserved across restart, avoiding interference with another active request. Database failure postpones recovery; a stopped server cannot recover until restarted. Provider work/charges cannot be reliably cancelled after a lost connection or process crash.

## Configuration

Backend only: OPENAI_API_KEY (required for generation only), OPENAI_MODEL=gpt-4.1-mini, OPENAI_TIMEOUT_SECONDS=45 (5–60 allowed). Existing CORS and SQLite settings are unchanged. Frontend only needs VITE_API_BASE_URL. No real credential was created, printed, embedded or transmitted during verification. Test keys are explicitly artificial placeholders and all test transport is mocked. The integration server was launched with an explicitly empty key and an isolated /tmp SQLite database.

Official SDK 3.16.2 is installed and pinned. The Python development lock was generated from the dedicated venv, not unrelated globally installed packages. Frontend lockfile was generated by successful npm install. Python 3.14.7 and Node 24.19.0 were used. These locks are exact version captures, not cross-platform wheel guarantees or hash-verified Python locks.

## Actual verification — 2026-10-20 Asia/Calcutta

| Check | Result |
| --- | --- |
| Backend dependency installation in .venv | Passed with declared requirements plus OpenAI SDK |
| Frontend npm installation | Passed; 123 packages installed and package-lock.json generated |
| .venv/bin/python -m pytest -q | **64 passed in 0.92s** (includes preserved Phase 1/2 suites) |
| PYTHONPATH=. .venv/bin/python tests/smoke_check.py | **10 passed in 0.101s**, supplementary only |
| .venv/bin/python -m pip check | Passed: no broken requirements |
| npm test | **17 passed across 3 files**, 616 ms |
| npm run typecheck | Passed |
| npm run build | Passed; Vite 8.3.3 production output generated |
| Backend server startup | Passed on 127.0.0.1:8000 with isolated test DB and empty API key |
| Frontend dev server startup | Passed on 127.0.0.1:5173 |
| Live local HTTP | Passed health, creation, retrieval, history, missing-key planning, unchanged pending state, allowed/denied CORS, frontend HTTP 200 |
| Actual browser flow | Passed health connection, empty state, real task submission/list/detail/history, Generate AI Plan -> clear missing-key error with pending state preserved |
| Desktop visual inspection | Task/detail/error panels inspected; no clipping observed in inspected viewport |
| Credential-pattern scan of source/tests | Passed; no API-key/private-key pattern matches |
| Live OpenAI generation | **Not attempted**; no configured live key used |
| Browser successful-plan flow | **Not attempted**; plan success/rendering tested with mocked SDK/component fixtures only |
| Dedicated mobile/keyboard/screen-reader audit | **Not attempted**; responsive styles and accessible controls retained |
| Public deployment / vulnerability audit | **Not attempted**; deployment outside scope |

No test assertion failures occurred in the executed suites. Two early commands were issued from the wrong working directory and failed before their intended operation; both were corrected. No unresolved test/build failure or dependency blocker remains. Earlier-phase installation restrictions are historical and were not repeated in Phase 3.

The 64 Pytest cases cover invalid schemas, explicit missing-key failure, SDK rate/network/auth/server/timeout errors, malformed/refused/incomplete response handling, exact saved history, restart persistence, duplicate/concurrent requests, unrelated writes during the API wait, stale token rejection, recovery idempotency, migration preservation, and atomic rollback. A real SDK parse call over httpx2.MockTransport verifies the actual SDK request schema/parsing without contacting OpenAI. Refusal is represented by a response with no parsed output; no live refusal was requested.

## Remaining limitations

- The default model/account access and real paid API generation still need a live smoke check with the user's backend key.
- No authentication, user isolation, global AI concurrency limit or spend budget. Run locally; public exposure is not part of this phase.
- Clarification questions are displayed; answer/edit/resubmit-plan workflows are not implemented. A new task can capture revised requirements.
- Existing awaiting_approval state is used only to mean ready for review; there is no approval or execution action.
- A lost provider response or server crash can cost tokens even if no plan was saved. Explicit retry may incur another charge. Pending-task creation still has no idempotency-key support.
- Recovery depends on a running server, writable SQLite and a consistent clock. Permanent storage failure prevents state/history recovery.
- Frontend displays the latest 20 tasks; API pagination remains available. History is returned in full.

## Exact local setup and run commands

From the project root:

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock.txt
[ -f .env ] || cp .env.example .env
# Edit backend/.env locally: set OPENAI_API_KEY; optionally change model/deadline.
python -m pytest -q
PYTHONPATH=. python tests/smoke_check.py
cd ../frontend
npm ci
[ -f .env ] || cp .env.example .env
npm test
npm run typecheck
npm run build
```

Terminal 1, from project root:

```sh
cd backend
source .venv/bin/activate
python -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Terminal 2, from project root:

```sh
cd frontend
npm run dev
```

Open http://127.0.0.1:5173. Task API/docs: http://127.0.0.1:8000/docs. No key is needed for tests or task management. Stop at Phase 3.
