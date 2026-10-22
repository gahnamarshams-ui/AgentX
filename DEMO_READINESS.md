# AgentX text-only hackathon demo

Latest update (2026-10-22): the exact exclusive-schedule conflict and explicit
resolution passed real OpenAI testing on verified PID 58771, pipeline revision
`exclusive-attribute-guard-v3`. Port 8002 and the frontend on port 5174 are running;
both execution flags are false. See **Verified live request-path repair** below.
Earlier entries describe prior versions and remain as historical evidence.

## Prior demo status — before the latest conflict fix

Verified 2026-10-21. No remaining blocker for these three scenarios. The demo
backend on `127.0.0.1:8002` was gracefully restarted to load the latest Verifier;
Vite on `127.0.0.1:5174` also needed a restart because it was serving cached older
activity/client modules. Both are now running the current code. The independent
backend on port 8001 was left untouched. No application code changes were needed
in this finishing pass; this guide and the live evidence JSON were updated.

The frontend and health endpoint returned HTTP 200, CORS allowed the exact
frontend origin, and the read-only execution endpoint returned HTTP 503 with
`execution_disabled`. Both execution flags were explicitly false at launch.
The served frontend modules now contain the structured Verifier report and its
client validation. This is local development readiness, not public deployment
or production-safe execution readiness.

Manual step: open <http://127.0.0.1:5174/> and hard-refresh once to discard old
browser modules. Select one of the newest three saved demo conversations and
expand **Show agent activity**. In the code example, Python syntax/function
checks should be `passed`, correctness checks `not_checked`, and execution and
factual-verification fields false. This final visual presentation check remains
manual; frontend behavior is otherwise covered by the existing jsdom tests.

## Run

The existing backend on port 8001 was left untouched. This demo uses a separate,
persistent database (`backend/data/hackathon-demo.sqlite3`). Do not run a second
server on a port already in use. The commands below are for launching again
after stopping the corresponding demo server with Ctrl+C in its terminal.
These commands do not change `.env`.

Backend terminal:

```sh
cd /Users/yagow/Desktop/agent-X/backend
EXECUTION_ENABLED=false PYTHON_EXECUTION_ENABLED=false DATABASE_PATH=data/hackathon-demo.sqlite3 CORS_ORIGINS='["http://127.0.0.1:5174","http://localhost:5174"]' .venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8002
```

Frontend terminal:

```sh
cd /Users/yagow/Desktop/agent-X/frontend
VITE_API_BASE_URL=http://127.0.0.1:8002 npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

Open <http://127.0.0.1:5174/>. Saved conversations restore their answer and
activity after refresh. Expand **Show agent activity** to see backend records.

## Repeat the three scenarios

```sh
cd /Users/yagow/Desktop/agent-X/backend
.venv/bin/python demo_text_workflow.py
```

This intentionally makes six real OpenAI requests (Planner and Executor for each
new task), creates three conversations, and saves API/SQLite evidence in
`backend/data/hackathon-demo-evidence.json`. It never submits approvals, calls
execution endpoints, evaluates returned code, or invokes workspace tools. It
requires the demo server/database above. Existing evidence is replaced only
after all three scenarios pass. Review failures in the saved task history;
do not blindly resubmit a response whose ownership is still active.

| Scenario | Prompt | Observed result | Saved task |
| --- | --- | --- | --- |
| A | Calculate 125 + 275. Reply with only the numeric answer. | `400` | `1fa11a14-cd57-4adb-9904-02642498e8b0` |
| B | Show a Python function add(a, b) that returns their sum. Show code only; do not execute it. | Fenced function with `return a + b`; syntax/name checks passed, never run | `337f7724-9234-49bc-9364-75e1f5bff69b` |
| C | Inspect my local workspace files and report what they contain. If you cannot access them, explain that limitation without inventing file contents. | Stated it cannot access local files and requested supplied contents | `7b823543-0e32-4f10-80ec-5471a6671e35` |

Scenarios B/C were selected as harmless defaults after the original request
ended during Scenario A. Scenario C's actual answer was manually reviewed;
keyword checks are not treated as proof of truthful answers.

Each live task completed with exactly five persisted successful roles, a saved
internal plan, matching answer SHA-256, and one running/succeeded transition per
role in history. Its execution-run, step, approval and Python-job counts were
zero. The latest harness completed all three scenarios with the structured
Verifier assertions enabled. Its JSON task/activity/history records were then
compared with fresh API reads and SQLite counts; all matched. Internal plans
and answer hashes were confirmed. Scenario C's actual answer was reviewed again
and did not claim file inspection. Previous demo conversations were retained.
The frontend and backend health returned HTTP 200; backend CORS allowed
the exact frontend origin. These observations support a text-only demo, not a
live tool-execution or sandbox-isolation claim.

## What the five roles actually do

| Role | Actual operation | Evidence |
| --- | --- | --- |
| Orchestrator | Deterministic coordination and ownership-fenced persistence | Status transitions, saved task, role summaries |
| Planner | Real OpenAI structured plan generation, schema validation | Internal plan in `chat_runs.plan_json`, goal summary |
| Safety | Deterministic schema, secret and text-only permission checks | Saved checks; no tool authorization |
| Executor | Real OpenAI text/code generation, or deterministic rendering when Planner requests clarification; no tools | Saved answer and an operation-specific summary |
| Verifier | Deterministic integrity checks; conservative Python syntax and explicit function-name checks | Saved check statuses, SHA-256 and text verification report |

Verifier does **not** prove arithmetic, factual accuracy, generated-code
correctness or sandbox isolation. Scenario A's expected result was separately
compared with fixed arithmetic in the demo harness. Text completion is distinct
from workspace execution: `/respond` never approves a tool plan. `/plan` creates
a proposal only; `/orchestrate` and `/approval` are separate, guarded workflows.
Both execution flags remain false, and Docker preflight remains unconditionally
blocked. Production-safe execution and live sandbox validation are future work.

## Changes and verification

- `backend/app/responses.py`: atomically fence initial activity persistence;
  record changed role states only, including skipped roles; persist Safety's
  running state; explicitly state the Verifier's factual-check limitation.
- `backend/app/planning.py`: persist interrupted chat-role failures/skips and
  matching history during lease recovery; never replay work.
- `frontend/src/PlanPanel.tsx`, `AgentProgress.tsx`: remove stale completed-chat
  Thinking/Ready headings; label activity as workflow rather than execution.
- Regression tests: `backend/tests/test_responses.py`,
  `frontend/src/PlanPanel.test.tsx`, `AgentProgress.test.tsx`.
- Reproduction and evidence: this guide, `backend/demo_text_workflow.py`, demo
  database and JSON evidence. No configuration/defaults, approval digest,
  execution tools, native cutoff provider or sandbox runtime were changed.

Initial demo test commands and results (historical; latest regression results
are recorded in the Verifier section below):

```sh
# backend: 210 passed
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_responses.py tests/test_planning.py tests/test_tasks.py tests/test_foundation.py tests/test_conversations.py tests/test_deadlines.py tests/test_python_sandbox.py
# backend: 8 passed; disposable fixtures, mocked executors, zero tool calls
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_execution.py::test_preparation_never_calls_tools_and_persists_digest tests/test_execution.py::test_read_only_invalid_approval_never_runs_tools tests/test_execution.py::test_read_only_cannot_bypass_gate_via_state_or_executor
# frontend: 65 passed; TypeScript and build passed
npm test
npm run typecheck
npm run build
```

The remaining filesystem-executing integration tests were not run. No generated
Python ran. Frontend behavior was tested in jsdom, not visually in a real browser.
Build still reports a nonblocking bundle-size warning (about 542 kB main JS).
OpenAI availability/quota remains a live dependency; model answers can vary.

## Focused Verifier improvement

New chat records now include `Verifier.text_verification`, with categorized
checks (`integrity`, `static_code`, `correctness`) and explicit `passed`, `failed`
or `not_checked` statuses. `code_executed` and `factual_correctness_verified`
are both false. The original string checks remain compatible. Reports persist
on both successful responses and rejected answers and appear in agent activity.
No database migration or historical-record rewriting is needed.

Only explicitly labelled `python`, `py` or `python3` fences are parsed with
`ast.parse`. No imports, evaluation, code execution, workspace access or tools
are involved. Syntax failures and missing explicitly requested top-level function
definitions reject the answer. Comments, strings and nested definitions do not
satisfy a requested top-level function. Unsupported/unlabelled blocks and
ambiguous function requests remain `not_checked`. Parsing uses the backend's
Python grammar; it does not validate compatibility with other Python versions,
imports, dependencies, types, behavior, security or factual correctness.

Changed files for this focused improvement:
`backend/app/responses.py`, `backend/app/execution/models.py`,
`backend/tests/test_responses.py`, `backend/demo_text_workflow.py`,
`frontend/src/api/client.ts`, `frontend/src/AgentProgress.tsx`,
`frontend/src/AgentProgress.test.tsx`, and this guide.

Exact verification commands (run from their respective backend/frontend directories):

```sh
# Focused backend: 27 passed
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_responses.py
# Relevant backend regressions including mocked tool-gate tests: 236 passed
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_responses.py tests/test_planning.py tests/test_tasks.py tests/test_foundation.py tests/test_conversations.py tests/test_deadlines.py tests/test_python_sandbox.py tests/test_execution.py::test_preparation_never_calls_tools_and_persists_digest tests/test_execution.py::test_read_only_invalid_approval_never_runs_tools tests/test_execution.py::test_read_only_cannot_bypass_gate_via_state_or_executor
# Focused frontend: 6 passed
npm test -- src/AgentProgress.test.tsx
# Full frontend: 66 passed; TypeScript and production build passed
npm test && npm run typecheck && npm run build
```

The production build retained its nonblocking main-bundle warning (~543 kB).
The implementation pass above used mocks only. The subsequent final readiness
pass restarted the demo services and genuinely exercised the latest Verifier
through live text-only OpenAI responses, producing the three records above.
Older records remain unchanged and are not assigned checks that never ran.

## Final readiness validation

Only the focused backend tests were rerun; no application source changed after
the previous passing regression, TypeScript and production-build checks.

```sh
cd /Users/yagow/Desktop/agent-X/backend
# Rerun: 27 passed in 0.75s
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_responses.py
# Rerun against restarted backend: all three scenarios passed
.venv/bin/python demo_text_workflow.py
```

Reused results: 236 backend regressions, 66 full frontend tests, TypeScript and
production build passed in the preceding Verifier implementation pass. Exact
commands are preserved above. No broad suite was unnecessarily repeated.

Remaining limitations: OpenAI connectivity/quota and variable model output;
conservative static checks are not semantic, security or factual verification;
no generated-code execution or sandbox-isolation guarantee; no real-browser
visual review in this finishing pass; nonblocking bundle-size warning. No
approvals, execution POSTs, generated-code execution or user workspace access
were performed. No installation or runtime provisioning is required for this
text-only demo. Both servers must remain running during the presentation.

## Continuous multi-turn chat

Root cause: every Send previously created an unrelated task and replaced the
displayed task; no database association linked turns, and Planner/Executor only
received the current request. `/respond` intentionally rejects rerunning a
completed task. Reusing a completed task ID would have overwritten its evidence.

The fix preserves a distinct task, answer, plan and five-role activity/history
for each turn. Additive migration 8 adds nullable `tasks.conversation_id`, indexed
and foreign-keyed to the first task. Legacy tasks remain independent roots.
`POST /api/tasks` accepts an optional `conversation_id`; root/child IDs normalize
to the same root. `GET /api/tasks/{id}/turns` restores saved turns in order.
The general task list retains individual tasks; `conversations_only=true`
returns sidebar roots and searches across their turns. Root metadata controls
the conversation; confirmed deletion removes all child records transactionally
and checks every turn for active/uncertain work first. Individual child deletion
is rejected, preventing partial, misleading conversation history.

Each new turn calls the existing `/respond` five-role workflow. Both real model
calls receive the saved previous user/assistant messages as untrusted context;
failed turns have no fabricated assistant response. Internal plans, approvals
and tool records are not promoted into chat context. The context has a 96,000
serialized-character limit; larger conversations fail clearly and ask for New
Chat, retaining saved history. The current prompt still controls requested
function-name checks. Existing Safety, Verifier, execution guards, disabled flags
and unconditional Docker activation block are preserved.

SQLite write transactions serialize follow-up creation and planning ownership.
Only one pending/running turn is allowed per conversation; duplicate response
starts are rejected. Earlier turns cannot be retried after a later turn is
saved, so subsequent context is not silently rewritten. The composer locks
during a turn, re-enables after completion/failure, preserves drafts on creation
errors, and rejects empty input. After refresh, reopen the conversation from the
sidebar to restore all turns; content is persisted in SQLite, not browser state.

Changed files:

- Backend: `app/database.py`, `schemas.py`, `tasks.py`, `planning.py`,
  `responses.py`, and the task-create validation message in `main.py`.
- Backend tests: `tests/test_responses.py`, `test_tasks.py`, and the migration
  version expectation in `smoke_check.py` (the smoke script itself was not run).
- Frontend: `src/TaskWorkspace.tsx`, `Composer.tsx`, `api/client.ts`;
  new `MultiTurnChat.test.tsx` and updated API fixtures in `Conversation.test.tsx`,
  `ConversationManagement.test.tsx`, `TaskWorkspace.test.tsx`.
- This guide and `backend/data/multiturn-demo-evidence.json`.

Actual verification commands/results:

```sh
# backend: focused multi-turn, migration, metadata and persistence regressions — 66 passed
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_responses.py tests/test_tasks.py tests/test_conversations.py
# backend: final response/planning checks after the historical-turn fence — 59 passed
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_responses.py tests/test_planning.py
# backend: final safe regression selection — 239 passed
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_responses.py tests/test_planning.py tests/test_tasks.py tests/test_foundation.py tests/test_conversations.py tests/test_deadlines.py tests/test_python_sandbox.py tests/test_execution.py::test_preparation_never_calls_tools_and_persists_digest tests/test_execution.py::test_read_only_invalid_approval_never_runs_tools tests/test_execution.py::test_read_only_cannot_bypass_gate_via_state_or_executor
# frontend: focused two-turn/reload/duplicate/error tests — 2 passed
npm test -- src/MultiTurnChat.test.tsx && npm run typecheck
# frontend: final suite — 68 passed; TypeScript and production build passed
npm test && npm run typecheck && npm run build
```

Mock tests prove the persisted history is supplied to both model requests,
separate five-role records survive a new app instance, simultaneous follow-up
creation yields one success/one conflict, duplicate starts make no new model
calls, old-turn retries are blocked, and grouped deletion rolls back atomically.
No generated Python or live workspace tools were run. The build retains its
nonblocking main-bundle warning (~544 kB).

Live text-only verification also passed after the demo services restarted:
conversation `52c8d6da-3113-46f8-b600-ff18bf2988d0` answered `Noted.` to
“Remember the demo code word comet. Reply only with Noted.” Its follow-up task
`8d37976d-6a3a-4ffc-9e0c-84874cf7804d` answered `comet` to
“What demo code word did I ask you to remember? Reply only with that word.”
Both completed with five successful persisted roles, matching answer hashes,
separate histories and false execution/factual-verification fields. Their
execution-run, step, approval and Python-job record counts were zero. The
evidence file contains the saved tasks, activities and histories. Backend health,
frontend HTTP and exact-origin CORS checks passed; served frontend modules
include the new conversation client/UI. The existing demo DB migrated to version
8, retaining earlier conversations. The separate port-8001 server was untouched.

Manual check: open <http://127.0.0.1:5174/>, hard-refresh, choose New Chat, send
“My favorite color is blue,” wait for completion, then send “What is my favorite
color?” in the same composer. Both exchanges should stay visible. Refresh,
reopen that chat from the sidebar, and inspect each turn's agent activity and
task history. Only Planner and Executor make model calls. Code is displayed and
statically checked only; factual/semantic correctness is not established.

## Focused clarification regressions

Added `backend/tests/test_clarification.py` before changing implementation. All
five initial tests failed: a Planner-provided conflict question was discarded
in favor of the mocked answer model's arbitrary choice. The minimal fix in
`backend/app/responses.py` renders validated Planner clarification questions
directly, then applies the existing Verifier and persistence flow. Executor
activity explicitly says no answer-model call occurred, and Orchestrator says
user input is still required. A clarification response is a completed text
turn, not a claim that the underlying requirement conflict has been resolved.
Normal answer generation is unchanged. No schema/API/UI changes were needed.

Exact new test names, all passing:

- `test_conflicting_plan_requests_clarification_without_silently_selecting_rule`
- `test_user_clarification_resolves_conflict_in_same_conversation`
- `test_resolved_requirement_is_retained_after_reload_in_next_turn`
- `test_new_requirement_preserves_earlier_resolved_requirements`
- `test_duplicate_clarification_submissions_do_not_repeat_model_calls_or_history`
- `test_clarification_output_still_passes_secret_and_verifier_checks[secret]`
- `test_clarification_output_still_passes_secret_and_verifier_checks[malformed_fence]`

The fixture uses mocked SDK responses, temporary SQLite, explicitly disabled
execution flags, and hard failures on runtime/workspace tool calls. It checks
both model request payloads for original requirements and the user's resolution,
three-turn reload persistence, immutable earlier plans/answers, five-role
records, concurrent turn creation, and duplicates during/after a pending model
request. Conflict recognition and semantic resolution are supplied by mocked
Planner results; these tests do not prove general natural-language conflict
detection, model compliance or semantic correctness.

Focused command: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -v -p no:cacheprovider tests/test_clarification.py`
— 7 passed. Relevant backend regression command:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_clarification.py tests/test_responses.py tests/test_planning.py tests/test_tasks.py tests/test_foundation.py tests/test_conversations.py tests/test_deadlines.py tests/test_python_sandbox.py
```

Result: 238 passed in 3.33s. Existing sandbox regressions use fake runtimes;
they do not establish real sandbox isolation or enable the running demo.

Frontend regressions used the existing full suite, with caches outside the
project — 68 passed:

```sh
node --input-type=module -e 'import { startVitest } from "vitest/node"; const ctx = await startVitest("test", [], { run: true, cache: false, configLoader: "runner" }, { cacheDir: `/tmp/agentx-clarification-vite-${process.pid}` }); await ctx.close();'
```

Changed files for this phase: the new test module, `backend/app/responses.py`,
and this guide. No live model calls, server restarts, approvals, generated-code
execution or user workspace operations were performed. Restart the local demo
backend with both flags false before demonstrating this new clarification path;
the earlier saved live evidence does not validate this change.

## Live exclusive-conflict remediation

The exact failing prompt was located in persisted turn
`6f58a2c1-d064-4d8d-bf8b-67fb9131f762`. Its saved Planner flag was already
`clarification_needed=true`, but its questions concerned launch type, audience,
times/location and extra details, not the exclusive Monday/Tuesday conflict.
Executor nevertheless generated the two-day notice. That behavior contradicts
the current clarification-rendering branch, indicating older code in the running
backend. This record was inspected read-only and was not altered.

Request-path diagnosis: Orchestrator passed the saved request to Planner;
Planner missed the semantic contradiction; Safety checked schema/secrets/tool
permissions without a requirement-consistency rule; the older Executor generated
the artifact despite the clarification flag. Verifier passed formatting/integrity
checks and correctly left semantic/factual correctness unverified. Those checks
could not establish whether both exclusive schedule requirements were satisfiable.

Changes are restricted to:

- `backend/app/chat_conflicts.py`: new deterministic, domain-independent literal
  rule. A shared explicit attribute assigned two distinct values using
  `exclusively/only X and exclusively/only Y` is unresolved. Values are not
  hardcoded to days. Saved user messages are processed in order; only a clear
  positive user choice resolves a detected conflict. Assistant suggestions,
  ambiguous/conditional choices and conflicting negative replies do not.
- `backend/app/responses.py`: Safety records the matching attribute and request
  span in `exclusive_conflict:` checks, prioritizes the missing choice question
  in the saved plan even if Planner asked unrelated questions, and gates final
  artifact generation through the existing clarification-only Executor path.
  Existing Planner questions are preserved within the plan's bounded question
  list. Verifier remains responsible for its existing checks, not a new claim of
  semantic verification. Normal context persistence and execution gates remain.
- `backend/app/ai.py`: planning instructions explicitly require checking conflicts,
  asking rather than merging exclusive values, honoring explicit user resolutions,
  and retaining unrelated earlier requirements.
- `backend/tests/test_clarification.py`: added exact-prompt regressions with both
  false and unrelated true Planner clarification flags; other attribute/value
  pairs; unresolved conflicts across turns; ambiguous choices; explicit resolutions;
  unrelated questions; and nonconflicting/quoted/negated inputs. Existing tests
  retain coverage of three-turn history, original requirements and duplicate calls.
- This guide. No schema, frontend, approval digest, defaults or runtime-provider
  changes were made.

New regression test groups:

- `test_explicit_exclusive_conflict_is_caught_even_when_planner_misses_it`
- `test_missed_conflict_stays_blocked_until_explicit_user_resolution`
- `test_exclusivity_guard_does_not_invent_conflicts_from_ambiguous_or_nonconflicting_text`
- `test_unrelated_planner_clarification_does_not_hide_detected_conflict`
- `test_ambiguous_user_choice_does_not_resolve_an_exclusive_conflict`
- `test_explicit_positive_choice_resolves_conflict_without_dropping_other_requirements`

Tests first reproduced five failures before implementation. Final commands and
actual results, from the respective backend/frontend directories:

```sh
# Focused: 32 passed
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_clarification.py
# Safe backend regressions: 263 passed in 4.00s
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_clarification.py tests/test_responses.py tests/test_planning.py tests/test_tasks.py tests/test_foundation.py tests/test_conversations.py tests/test_deadlines.py tests/test_python_sandbox.py
# Existing frontend suite: 68 passed
node --input-type=module -e 'import { startVitest } from "vitest/node"; const ctx = await startVitest("test", [], { run: true, cache: false, configLoader: "runner" }, { cacheDir: `/tmp/agentx-conflicts-vite-${process.pid}` }); await ctx.close();'
```

The literal detector and Safety path run for real in these tests while model
responses are mocked, including false-negative Planner responses. This supports
the specified grammar, not general natural-language conflict detection or live
model reliability. The parser is conservative: unlabelled/implicit constraints,
other languages, numerical feasibility, paraphrases outside its grammar, complex
scopes/quoted values, longer exclusive lists and nonstandard resolutions may be
missed or require extra clarification. Such cases still depend on Planner; absence
of a rule match is not proof that requirements are consistent. No final-response
semantic verifier has been added. No live model call, generated-code execution,
workspace tool operation or deployment was performed.

Runtime: no active tasks or uncertain sandbox jobs were present before attempting
a graceful refresh. macOS rejected `kill -TERM 54075` with operation-not-permitted;
the attempted replacement exited with address-in-use and did not replace the
server. No force kill or escalation was attempted. The existing server remains
healthy and its execution guard still returns 503 `execution_disabled`. The new
code has NOT been live-validated in that process.

Required manual step: Ctrl+C in the terminal owning the port-8002 backend, then:

```sh
cd /Users/yagow/Desktop/agent-X/backend
EXECUTION_ENABLED=false PYTHON_EXECUTION_ENABLED=false DATABASE_PATH=data/hackathon-demo.sqlite3 CORS_ORIGINS='["http://127.0.0.1:5174","http://localhost:5174"]' .venv/bin/python -B -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8002
```

Frontend stays at <http://127.0.0.1:5174/>. Start New Chat and repeat the exact
reported prompt. Expect a choice question naming Monday and Tuesday before any
notice, with Safety's conflicting-span evidence visible. Reply “Use Tuesday only
instead of Monday. Keep the other requirements.” Then add plain-text formatting
in the next turn. Check title Launch, English, maximum 80 words and Tuesday are
retained. Do not approve plans or enable either execution flag. That live manual
test remains outstanding.

## Verified live request-path repair

The latest failing exact-prompt record was
`50437ff2-cf2f-4fa2-82bb-b6689629fa5d`. Its Safety checks contained only the three
legacy permission checks, without `explicit_exclusivity_rules_checked` or any
`exclusive_conflict:` evidence. Its Planner flag was true for unrelated questions,
yet Executor generated the two-day notice. The new detector was not on that
request path. At initial inspection in this pass, no process was listening on
port 8002 and health requests were refused; no successful restart was assumed.

A fresh v2 process was started with both flags false and inspected through the
new runtime endpoint. Live testing then exposed an additional bug in the previous
fix: a question asking for dates/times "on Monday and Tuesday" contained both
values and a question mark, so the question-matching heuristic incorrectly treated
it as a choice. Safety detected the conflict and blocked artifact generation but
did not ask which value to use. A regression reproduced that precise error before
the heuristic was removed.

The v3 fix always puts Safety's deterministic exclusive-choice question first,
then retains other Planner questions, deduplicating exact strings only. Executor
independently recomputes unresolved literal conflicts before a final answer-model
call and fails closed if a conflicting plan reaches it without clarification.
This protects the final-generation boundary even if Safety review regresses.
Orchestrator persists the pipeline revision; Executor persists whether final
generation was blocked and whether its answer-model call was skipped/completed.
Verifier retains its existing integrity/static checks and unverified-correctness
fields. No execution or approval boundary was changed.

`GET /api/v1/runtime` is a read-only diagnostic exposing only process ID, declared
chat pipeline revision, startup/current application-source fingerprints, a
`restart_required` flag and the two actual execution flags. It does not expose
secrets, paths or environment contents. Health API compatibility is unchanged.
Source changes were observed to set `restart_required=true` in the old v2 process
before it was gracefully stopped through its owned terminal. The v3 process was
then started and verified before live requests. The source fingerprint is an
operational startup snapshot, not signed binary attestation or a sandbox guarantee.

New regression cases (tests first; the missing Executor block, missing diagnostic
endpoint, and misleading dates question each failed before the relevant fix):

- `test_executor_refuses_exact_unresolved_conflict_even_if_safety_was_bypassed`
- `test_executor_allows_explicit_resolution_and_retains_original_context`
- `test_http_request_cannot_generate_artifact_if_safety_conflict_review_regresses`
- `test_dates_question_mentioning_both_values_cannot_replace_required_choice`
- `test_runtime_reports_process_revision_and_actual_disabled_flags_without_secrets`
- `test_runtime_detects_source_changed_since_process_import`

Earlier question-preservation assertions now also verify the required choice is
first and that the whole earlier saved plan stays immutable across later turns.

Final exact commands/results:

```sh
# backend focused: 50 passed
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_clarification.py tests/test_foundation.py
# full relevant safe backend selection: 269 passed in 5.08s
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_clarification.py tests/test_responses.py tests/test_planning.py tests/test_tasks.py tests/test_foundation.py tests/test_conversations.py tests/test_deadlines.py tests/test_python_sandbox.py
# full frontend: 68 passed
node --input-type=module -e 'import { startVitest } from "vitest/node"; const ctx = await startVitest("test", [], { run: true, cache: false, configLoader: "runner" }, { cacheDir: `/tmp/agentx-live-path-final-vite-${process.pid}` }); await ctx.close();'
```

Real live observations, captured in `backend/data/conflict-live-evidence.json`:

- Process 58771, revision `exclusive-attribute-guard-v3`, startup/current source
  SHA-256 `c1e73773f6406ff2cd29ef680e7b1a3d5cf7cdab34017ae8d03d7726dd43dcca`,
  `restart_required=false`, `execution_enabled=false`, `python_execution_enabled=false`.
- Exact prompt: "Write an English notice titled Launch, maximum 80 words,
  scheduled exclusively Monday and exclusively Tuesday."
- Turn `ae246557-5fdf-4f50-b595-611894c2a509` returned first: "The same scheduled
  cannot be exclusively both Monday and Tuesday. Which should I use?"
  Its saved answer equals the clarification questions, not a notice. Safety saved
  `exclusive_conflict:scheduled: scheduled exclusively Monday and exclusively Tuesday`.
  Executor saved `final_artifact_generation:blocked_pending_clarification` and
  `answer_model_call:skipped`.
- Same-conversation turn `af7947a0-3611-4608-a102-69d404ee451c` used: "Use Tuesday
  only instead of Monday. This is a product launch for customers. Omit time,
  location and contact details. Retain title Launch, English and maximum 80 words.
  Write the notice." Safety no longer found an unresolved conflict, Executor
  recorded its completed model call, and the response was an English Launch
  notice scheduling Tuesday only, 69 whitespace-counted words including its title.
- Both turns had exactly five successful persisted role records, matching answer
  hashes, separate histories, `code_executed=false` and
  `factual_correctness_verified=false`. Execution-run, step, approval and sandbox-job
  counts for these turns were zero. Process/revision/fingerprints/flags were checked
  unchanged after the requests. The word count/requirements were checked by the
  live harness; they are not new Verifier guarantees.

Changed files in this pass: `backend/app/responses.py`, `backend/app/main.py`,
`backend/tests/test_clarification.py`, `backend/tests/test_foundation.py`, this
guide, and the live evidence JSON. No frontend source, .env, execution defaults,
schema, approved file workflow or native sandbox provider was changed. No generated
code or workspace tools ran; no deployment occurred. The real model results prove
these observed requests only, not general natural-language or future model reliability.
The literal grammar and conservative resolution limitations above still apply.

Restart/version verification for future manual runs:

```sh
curl -sS http://127.0.0.1:8002/api/v1/runtime
```

Require revision `exclusive-attribute-guard-v3`, `restart_required=false`, matching
fingerprints and both flags false. A missing endpoint, old revision, mismatched
fingerprints or wrong flags means stop that backend with Ctrl+C in its terminal,
then launch exactly:

```sh
cd /Users/yagow/Desktop/agent-X/backend
EXECUTION_ENABLED=false PYTHON_EXECUTION_ENABLED=false DATABASE_PATH=data/hackathon-demo.sqlite3 CORS_ORIGINS='["http://127.0.0.1:5174","http://localhost:5174"]' .venv/bin/python -B -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8002
```

Query runtime again; do not assume restart success if the port was already in use.
No restart is presently needed. Open <http://127.0.0.1:5174/>, refresh and select
the newest exact-prompt conversation to inspect both saved turns and gate evidence.
