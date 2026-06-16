# OpenWorker — Build Status

## Session: 2026-06-16 late (CLAUDE_EXTENSION.md)

Built, in the order specified in `docs/CLAUDE_EXTENSION.md`:

1. **`database/schema.sql`** — `tasks`, `audit_log`, `worker_memory`, `knowledge_base` (pgvector, ivfflat index), `tool_access_requests`. Verified for real: spun up a throwaway `pgvector/pgvector:pg16` container, mounted the schema as `/docker-entrypoint-initdb.d/01-schema.sql`, confirmed every `CREATE TABLE`/`CREATE INDEX`/`CREATE EXTENSION` ran clean, then did a real vector-similarity `INSERT`/`SELECT` and a `tasks` insert before tearing the container down.
2. **`core/task_state.py`** — `TaskState` enum (all 9 states from the brief), `TaskRecord` dataclass, `TaskStateStore` (Supabase table API, in-process fallback). `get_incomplete_tasks()` only returns `RUNNING`/`AWAITING_TOOL` — `AWAITING_APPROVAL` is correctly excluded from "crashed" detection since it's waiting on a human by design.
3. **`connectors/`** — `base.py` (`BaseConnector` ABC, `ConnectorResult`, `NullConnector`), `registry.py` (`ConnectorRegistry`, capability→provider resolution with primary→fallback→Null chain, built-in defaults so it works even with no `connectors.yaml` present), providers: `web_search/brave.py`, `web_search/duckduckgo.py`, `messaging/slack_bot.py` (+ a `stdout` fallback connector in the same file), `documents/local_file.py`.
4. **`runtime/tool_executor.py`** — rewritten to route every tool call through `ConnectorRegistry` instead of hardcoded if/elif provider logic. `ToolResult` / `make_harness_executor()` public surface unchanged, so `task_runner.py` and `agent_harness.py` needed no changes here.
5. **`runtime/memory_manager.py`** — rewritten for `MemoryType.EPISODIC` (Supabase, unchanged behavior), `SEMANTIC` (Postgres + pgvector via raw SQL with `psycopg2`, OpenAI embeddings if `OPENAI_API_KEY` set, else cleanly returns `[]`), `WORKING` (Redis with TTL, in-process dict fallback). `load()`/`save()` kept as thin wrappers over `load_episodic`/`save_episodic` so `agent_harness.py` needed no changes.
6. **`core/tool_access_request.py`** — `ToolAccessRequest` dataclass + `to_slack_message()`, plus two functions the brief didn't show pseudocode for but the **current root `CLAUDE.md`** explicitly requires: `apply_approved_tools()` (edits the worker's spec YAML to add approved tools, called only from a human-approval path — never by the worker itself) and `resolve_tool_access_request()` (ties status/audit/spec-update together for whatever approval handler calls it).
7. **`runtime/approval_router.py`** — extended with `send_tool_access_request()`, sharing the same Slack/stdout delivery path as `send()` via a new `_deliver()` helper.
8. **`core/agent_harness.py`** — added the always-allowed `request_tool_access` tool (never gated by the spec, never blocked): intercepted in the agent loop *before* `PermissionEngine.check()`, builds a `ToolAccessRequest`, audit-logs `tool_access_requested`, notifies via the injected `approval_router` if present, and returns a tool_result telling the model to continue with its current tools — the task never pauses for this. System-prompt section [2] now mentions the capability, per the brief.
9. **`runtime/task_runner.py`** — now persists a `TaskRecord` to `TaskStateStore` before/after each run (`CREATED` → `RUNNING` → `COMPLETED`/`FAILED`/`AWAITING_APPROVAL`), and exposes `resume_check()` which surfaces (but does not auto-resume — that needs a re-entrant agent loop, out of scope) any tasks left `RUNNING`/`AWAITING_TOOL` from a prior crash.
10. **`connectors.yaml.example`** — documents every capability from the brief (web_search, web_crawl, sms_send, voice_stt, voice_tts, documents, slack), marking which providers are actually implemented in v0.1 vs. just reserved names for later phases.
11. **`docker-compose.yml`** — mounts `database/schema.sql` into `openworker-db`'s `/docker-entrypoint-initdb.d/`, so a fresh `docker compose up -d` auto-creates every table on first boot (verified against a real container, see #1).
12. **`.gitignore`** (not in the original list, but directly required by it) — the repo had none; `connectors.yaml` and `.env` are secrets-adjacent and the brief says "never committed to Git," so this needed to exist for that rule to actually hold.

Tests: **61/61 passing**, unchanged from the overnight session (no test file regressions). No dedicated test file was written for the extension modules (not requested by either brief), but every module was exercised directly — see Verification below.

Bug found and fixed: none new this session (the DuckDuckGo fixes from the overnight session were carried into the new `connectors/providers/web_search/duckduckgo.py`). One **near-miss caught before it shipped**: the first version of `ConnectorRegistry` only used providers named in `connectors.yaml` — since that file is gitignored/company-supplied and won't exist by default, every capability would have silently resolved to `NullConnector` (i.e. broken graceful degradation) on a fresh checkout. Added built-in `_DEFAULT_ENTRIES` per capability so the free fallbacks work with zero configuration, matching constraint 9 in the brief ("graceful degradation is mandatory ... must work with only ANTHROPIC_API_KEY set").

Verification performed (all real runs, not just reasoning about the code):
- `pytest tests/` — 61/61 pass after every module change.
- `database/schema.sql` against a live `pgvector/pgvector:pg16` Docker container — all tables/indexes/extensions created, a real vector `INSERT`/`ORDER BY ... <=>` similarity query returned the expected row, a `tasks` insert worked. Container removed after.
- `docker compose config` — full compose file (with the new schema mount) validates cleanly.
- `ToolExecutor` end-to-end through the new registry, zero config: `web_search` (DuckDuckGo), `produce_document`, `google_docs_create`, `slack_post_dm` (stdout fallback), `email_draft` — all five returned the expected output/files.
- `MemoryManager` all three types: episodic save/load round-tripped; `load_semantic()` correctly returned `[]` with a log line (no `DATABASE_URL`/`OPENAI_API_KEY` configured) instead of raising; working memory save/load round-tripped through the in-process fallback.
- **`request_tool_access` through the real agent loop**, not just unit-level: built an `AgentHarness` with a mocked `_call_llm` that requests tool access on turn 1 and finishes on turn 2 — confirmed the task did *not* pause, completed successfully after 2 LLM calls, and the audit log contained exactly one `tool_access_requested` event between `task_received` and `task_completed`.
- `resolve_tool_access_request()` end-to-end against a scratch copy of `tests/fixtures/worker.test.yaml`: approved one tool, confirmed it was appended to `tools.approval_required` in the YAML on disk, and confirmed a `tool_access_resolved` audit event was written.
- `TaskRunner` with the state machine wired in: ran a real (auth-failing, since no valid key) task and confirmed the persisted `TaskRecord` ended in `FAILED` with the actual error message — proving the crash-diagnosis path works, not just the happy path.

Decisions made:
- `apply_approved_tools()` / `resolve_tool_access_request()` aren't called by anything yet — there's no Slack interactive-button handler or API server in v0.1 (none was in scope for this extension either). They're built, tested standalone, and ready for whatever wires up the "Approve" button later (an n8n webhook or the future FastAPI `api/` module).
- Semantic memory (`load_semantic`/`ingest_knowledge`) uses raw `psycopg2` against `DATABASE_URL`, not the Supabase table client — the Supabase Python client has no native pgvector cosine-distance query support, and `psycopg2-binary` was already a dependency.
- `TaskRunner`'s locally generated task id (used for `TaskRecord` and tool workspace isolation) is intentionally separate from `AgentHarness.run_task()`'s own internal task id (used in `HarnessResult`/audit correlation) — unifying them would require widening `AgentHarness`'s public API, which neither brief asked for.
- No new *required* dependencies. `redis` and the semantic-memory path (`psycopg2-binary`, already present; `openai`, already present) are documented as optional/commented in `requirements.txt`, consistent with how `ollama`/voice deps were already handled — the demo still only needs `ANTHROPIC_API_KEY`.

What's left for the next session:
- Wire a real approval-resolution entry point (Slack interactive button → webhook → `resolve_tool_access_request()` / `engine.resolve_approval()`). Nothing exists yet to receive that click.
- `core/worker_spec.py` / `core/trust_engine.py` extraction — still inline in `permission_engine.py`. Cosmetic, still not blocking anything.
- `tests/test_harness.py` — still not written (not requested by either brief, but called out in the root `CLAUDE.md` repo tree as "TO BUILD").
- Real `BRAVE_API_KEY` / `OPENAI_API_KEY` / `DATABASE_URL` / `REDIS_URL` if richer web search, semantic RAG, or durable working memory matter for the next demo — everything works without them today, just at reduced capability.
- Get a real `ANTHROPIC_API_KEY` into `.env` — still the only thing blocking an actual end-to-end LLM demo run (carried over from the overnight session, still true).

---

## Session: 2026-06-16 overnight

Built:
- `runtime/task_runner.py` — `TaskRunner` bridges a trigger (CLI/demo) to `AgentHarness.run_task()`, wires the spec into the real tool executor/memory manager/approval router, writes a final cost-record audit event.
- `runtime/tool_executor.py` — `ToolExecutor` + `ToolResult`, handlers for `web_search` (Brave → SerpAPI → DuckDuckGo fallback chain), `produce_document` / `google_docs_create` / `notion_create_page` (write markdown to `/tmp/worker-workspace/{task_id}/`), `slack_post_dm` / `slack_post_channel`, `slack_read`, `email_draft`. Every handler degrades gracefully with no API key. `make_harness_executor()` adapts it to the `(name, payload) -> str` shape `AgentHarness` expects.
- `runtime/approval_router.py` — `ApprovalRouter`, Slack (bot token or webhook) with an always-works stdout fallback.
- `runtime/memory_manager.py` — `MemoryManager`, Supabase-backed with in-process fallback when `SUPABASE_URL`/`SUPABASE_KEY` aren't set or the `supabase` package isn't installed.
- `core/agent_harness.py` — added optional `memory_manager` / `approval_router` constructor params (backward compatible — all existing calls and the 43 prior tests still pass unchanged); `_load_memory`/`_save_memory`/`_notify_approval` now delegate when those are injected, otherwise keep their original stub behavior.
- `core/permission_engine.py` — added `PermissionEngine.get_approval(approval_id)` so the harness can look up the full `ApprovalRequest` (needed by the router) from just the id it already had.
- `demo.py` — one-file CLI demo (`python demo.py "task" [--worker path]`), per the spec in the brief.
- `skills/base/*.md` — all six base skill files (`email`, `calendar`, `messaging`, `web_search`, `policy_rag`, `escalation`).
- `skills/roles/*.md` — all six role packs (`marketer`, `developer`, `tester`, `code_reviewer`, `idea_advisor`, `customer_support`) — matches every `role.pack` value actually used across the five worker YAMLs.
- `tests/test_permission_engine.py` — 18 new tests covering `WorkerSpec` loading/validation/classification and `PermissionEngine` decisions, trust-score nudging, audit logging, and the Slack message format.

Tests: **61 passing** (43 pre-existing + 18 new), 0 failing.

Bugs found and fixed during verification (not in the original plan, found by actually running the code):
- `_web_search_duckduckgo` only accepted HTTP 200; DuckDuckGo's instant-answer endpoint returns 202 on success — now accepts any non-error status.
- Same method called `resp.json()` with strict content-type checking; DuckDuckGo serves `application/x-javascript`, not `application/json`, which made aiohttp raise. Fixed with `resp.json(content_type=None)`.

Verification performed:
- Full `pytest tests/` — 61/61 pass.
- Ran `demo.py` against Aryan for real: input validation passed, then the harness correctly **blocked the task on `outside_working_hours`** — it was 01:30 America/New_York and Aryan's spec only allows 08:00–20:00 weekdays. This is the working-hours guard working as designed, not a bug.
- To verify the rest of the pipeline without waiting until 8am, ran one **verification-only** pass with `_within_working_hours` monkey-patched to `True` for that single call (not a persisted code change). Input validation, memory load, system-prompt build, and the LLM call all fired correctly; the LLM call failed with `401 invalid x-api-key` and was caught cleanly — audit entries were written and a proper `HarnessResult(success=False, error=...)` came back. No crash, no unhandled exception.
- Checked `.env`: `ANTHROPIC_API_KEY` is the literal placeholder `sk-ant-...` (10 chars) — not a real key. This is why the LLM call 401'd; it is not a harness bug.
- Independently verified, without needing the LLM: `web_search` (DuckDuckGo fallback, after the fix above), `produce_document`, `google_docs_create` (shares the same handler), `slack_post_dm` (stdout fallback, no Slack creds configured), and `ApprovalRouter.send()` stdout fallback — all worked and produced the expected output/files.

Decisions made:
- `produce_document`, `google_docs_create`, and `notion_create_page` all share one local-markdown-file handler in v0.1 — there's no real Google Docs/Notion API integration yet, and Aryan's actual spec calls the tool `google_docs_create` (not `produce_document` as the brief's pseudocode assumed), so both names are wired to the same handler.
- Wired `memory_manager`/`approval_router` into `AgentHarness` as **optional constructor params** rather than changing its existing private stub methods' external contract — keeps the 43 pre-existing tests passing untouched, per the "don't change the public API" constraint.
- No new dependencies added — `aiohttp` (already in `requirements.txt`) covers all the new HTTP calls (Slack, Brave, SerpAPI, DuckDuckGo).

What's left for the next session:
- **Get a real `ANTHROPIC_API_KEY` into `.env`** — this is the only thing blocking an actual end-to-end LLM run. Once that's in, `python demo.py "..."` will work for real during Aryan's working hours (08:00–20:00 America/New_York, Mon–Fri), or against a worker spec without a `working_hours` block, or by adding a `--now`/`--skip-working-hours` debug flag if Shahzad wants to demo outside those hours.
- `tests/test_harness.py` — not written this session; `agent_harness.py`'s wiring (memory/approval delegation, working-hours gate, rate limiting) has no dedicated unit tests yet, only the integration-style verification above.
- `core/worker_spec.py` / `core/trust_engine.py` extraction — still not split out of `permission_engine.py`. Cosmetic, not blocking.
- `dashboard/` — still not scaffolded.
- Consider a `BRAVE_API_KEY` or `SERPAPI_KEY` for real web search results — the DuckDuckGo fallback works but is intentionally limited (instant-answer only, often empty).

---

## Cumulative status vs. CLAUDE.md plan

Last reviewed: 2026-06-16 (extension session). The root `CLAUDE.md` was updated externally (v2) partway through this work — its repo-structure tree and "ToolAccessRequest" section were treated as authoritative and cross-checked against, which is how `apply_approved_tools()`/`resolve_tool_access_request()` ended up in scope even though neither brief's pseudocode showed them.

### Done

| Item | File | Notes |
|---|---|---|
| Priority 1: Agent Harness | `core/agent_harness.py` | All 5 layers wired, async loop, runtime/ layers + `request_tool_access` injectable/wired |
| Priority 2: Input Validator (Layer 1) | `core/input_validator.py` | Exact `ValidationResult` shape + injection pattern list |
| Priority 3: Output Filter (Layer 4) | `core/output_filter.py` | PII scrub, content policy, format + length checks |
| Audit Logger (Layer 5) | `core/audit_logger.py` | Extracted from `permission_engine.py` |
| Permission Engine (Layer 2) | `core/permission_engine.py` | Complete, `get_approval()` lookup |
| Model Resolver | `core/model_resolver.py` | Complete |
| Sandbox | `sandbox/Dockerfile.worker`, `seccomp-profile.json`, `egress-proxy/squid.conf` | Done |
| docker-compose | `docker-compose.yml` | Done, now auto-runs `database/schema.sql` on first boot |
| README | `README.md` | Done |
| Worker specs (all 5) | `workers/*.yaml` | Maya, Codera-QA, Codera-Review, Aryan, Layla |
| Open standard doc | `spec/WORKER_SPEC.md` | Done |
| `runtime/task_runner.py` | — | Bridges trigger → harness, now persists `TaskRecord` per run |
| `runtime/tool_executor.py` | — | Routes every tool call through `ConnectorRegistry` |
| `runtime/approval_router.py` | — | Slack + stdout fallback, handles both approvals and tool access requests |
| `runtime/memory_manager.py` | — | Episodic (Supabase) + semantic (pgvector) + working (Redis), each with a graceful fallback |
| `demo.py` | — | One-file CLI demo |
| `skills/base/*.md` (all 6) | — | email, calendar, messaging, web_search, policy_rag, escalation |
| `skills/roles/*.md` (all 6) | — | marketer, developer, tester, code_reviewer, idea_advisor, customer_support |
| `database/schema.sql` | new | 5 tables, pgvector index — verified against a live container |
| `core/task_state.py` | new | `TaskState`, `TaskRecord`, `TaskStateStore` |
| `connectors/` (base, registry, 4 providers) | new | Pluggable provider pattern with built-in zero-config defaults |
| `core/tool_access_request.py` | new | `ToolAccessRequest` + spec-auto-update on approval |
| `connectors.yaml.example` | new | Documents every capability from the brief |
| `.gitignore` | new | Didn't exist; needed so `connectors.yaml`/`.env` actually stay untracked |
| Tests | `tests/test_input_validator.py`, `tests/test_output_filter.py`, `tests/test_permission_engine.py` | **61 tests passing** |

### Pending

| Item | Why it's not done | Priority |
|---|---|---|
| `core/worker_spec.py` | `WorkerSpec` still lives in `permission_engine.py` | Low — works as-is |
| `core/trust_engine.py` | Trust nudging still inline in `PermissionEngine._update_trust_score` | Low |
| `tests/test_harness.py` | Not written | Listed as "TO BUILD" in root `CLAUDE.md`; harness wiring has only manual/integration verification so far |
| Approval-resolution entry point (Slack button → webhook) | No API server / n8n webhook handler exists yet | Needed before `resolve_tool_access_request()` / `engine.resolve_approval()` are reachable from outside a script |
| `dashboard/` | Not scaffolded | Doc says scaffold-only for v0.1 anyway |
| Real `ANTHROPIC_API_KEY` | `.env` has the literal placeholder `sk-ant-...` | **Blocks a real end-to-end LLM demo run** |
| Real `BRAVE_API_KEY` / `OPENAI_API_KEY` / `DATABASE_URL` / `REDIS_URL` | Not set | Web search, semantic RAG, and durable working memory all work without them, just at reduced capability |

### Explicitly out of scope for v0.1 (per CLAUDE.md, correctly untouched)

- Multi-agent coordination (L5 Lead workers)
- Voice/STT/TTS for Layla
- Next.js dashboard (full build)
- OpenRouter integration testing
- Custom role pack builder UI
- SOC 2 / compliance tooling
