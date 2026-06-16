# OpenWorker — Build Status

Tracks progress against the plan in [CLAUDE.md](CLAUDE.md). Last reviewed: 2026-06-16.

## Done

| Item | File | Notes |
|---|---|---|
| Priority 1: Agent Harness | `core/agent_harness.py` | Wires all 5 layers, async loop, exact `HarnessResult` shape, system-prompt order per spec |
| Priority 2: Input Validator (Layer 1) | `core/input_validator.py` | Exact `ValidationResult` shape + injection pattern list |
| Priority 3: Output Filter (Layer 4) | `core/output_filter.py` | PII scrub, content policy, format + length checks, exact `FilterResult` shape |
| Audit Logger extraction (Layer 5) | `core/audit_logger.py` | Extracted from `permission_engine.py`, re-exported for back-compat |
| Permission Engine (Layer 2) | `core/permission_engine.py` | Complete from design session |
| Model Resolver | `core/model_resolver.py` | Complete from design session |
| Priority 4: Sandbox | `sandbox/Dockerfile.worker`, `seccomp-profile.json`, `egress-proxy/squid.conf` | Done |
| Priority 5: docker-compose | `docker-compose.yml` | Done |
| Priority 6: README | `README.md` | Answers what/why/how up top |
| Worker specs (all 5) | `workers/*.yaml` | Maya, Codera-QA, Codera-Review, Aryan, Layla |
| Open standard doc | `spec/WORKER_SPEC.md` | Done |
| Tests | `tests/test_input_validator.py`, `tests/test_output_filter.py` | 43 tests passing |

## Pending (not blocking v0.1 goal: one worker, one task, one approval, full audit trail)

| Item | Why it's not done | Priority |
|---|---|---|
| `core/worker_spec.py` | `WorkerSpec` still lives in `permission_engine.py`, not extracted per repo-structure plan | Low — works as-is |
| `core/trust_engine.py` | Trust nudging still inline in `PermissionEngine._update_trust_score` | Low |
| `runtime/task_runner.py` | Not started | Phase 2 |
| `runtime/approval_router.py` | Not started — harness has a logging stub (`_notify_approval`) | Phase 2 |
| `runtime/memory_manager.py` | Not started — harness has stubs (`_load_memory`/`_save_memory`) returning empty/no-op | Phase 2 |
| `runtime/tool_executor.py` | Not started — harness default executor raises `NotImplementedError` until one is wired | Phase 2 |
| `skills/base/*.md` (email, calendar, messaging, web_search, policy_rag, escalation) | None written | Needed before system-prompt sections [3] have real content |
| `skills/roles/*.md` (marketer, developer, tester, code_reviewer, idea_advisor, customer_support) | None written | Same as above, section [4] |
| `tests/test_permission_engine.py` | Not written | Should land before integration tests |
| `tests/test_harness.py` | Not written | Same |
| `dashboard/` | Not scaffolded | Doc says scaffold-only for v0.1, full build Phase 2 — still untouched |

## Explicitly out of scope for v0.1 (per CLAUDE.md, correctly untouched)

- Multi-agent coordination (L5 Lead workers)
- Voice/STT/TTS for Layla
- Next.js dashboard (full build)
- OpenRouter integration testing
- Custom role pack builder UI
- SOC 2 / compliance tooling
