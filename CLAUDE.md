# OpenWorker — CLAUDE.md
# Master brief for Claude Code (Opus)
# Updated: 2026-06-16 (v2 — incorporates architectural decisions from design session)
# Read this fully before writing a single line of code.

---

## What is OpenWorker?

OpenWorker is an open-source **AI Employment Framework**.

It is NOT another agent framework. It is the layer that lets an
organisation *employ* AI agents the same way they employ humans:
hire, onboard, assign role, grant permissions, supervise, evaluate,
promote, suspend.

The core insight: enterprises don't fear AI capability. They fear
AI without governance. OpenWorker is the governance layer.

One-liner: "Hire AI workers. Govern them like staff."

Competitor context:
- OpenClaw, CrewAI, LangGraph, DeepAgents = how to BUILD agents
- Squad (bradygaster) = AI dev team for coding only, no governance
- Viktor (viktor.com) = AI employee in Slack, closed SaaS, no governance
- Nextiva XBert = voice bot only, closed, no governance
- NVIDIA NemoClaw = enterprise security add-on (validates our thesis)
- 3E "AI Control Plane" = same idea, enterprise-only, expensive
- OpenWorker = open source, SMB-first, self-hosted, governed, deploy in 10 min

Domain: openworker.io (purchased 2026-06-16)
GitHub: github.com/openworker-io/openworker

---

## Core concepts

### Worker Spec (YAML) — files in Git, never in database
Every AI worker is defined by a YAML file. It combines:
- Identity (name, role, department, hire date)
- Org placement (reports_to, backup_approver)
- Autonomy level (L1 Intern → L5 Lead)
- Base skills (email, calendar, messaging — every worker gets these)
- Role skills (marketer, developer, tester, etc.)
- Tool permissions (allowed / approval_required / blocked)
- Knowledge (company policy docs via RAG)
- Cost config (monthly budget, per-task cap, human equivalent salary)
- LLM model config (provider, fallback, require_on_prem option)
- Meeting config (Recall.ai for Zoom/Teams/Meet, wake word, TTS voice)
- Audit config

Worker specs live in Git — not in a database. They are
infrastructure-as-code: version-controlled, PR-reviewed, auditable.

### Autonomy Levels
L1 Intern   — observe and suggest only, cannot execute
L2 Junior   — execute, ALL actions require human approval
L3 Mid      — execute low-risk freely, high-risk needs approval
L4 Senior   — execute most tasks, escalate exceptions only
L5 Lead     — can coordinate and assign other workers

Trust score (0-100) drives promotion between levels.
Promotion is always human-initiated based on trust score.

### Five Worker Templates (YAML complete)
1. Maya         — Marketing Assistant (reference implementation)
2. Codera-QA    — Software Tester
3. Codera-Review — Code Reviewer
4. Aryan        — Idea → Plan Advisor (first demo target)
5. Layla        — Customer Support (voice-capable, Phase 3)

### Five Harness Layers (inside-out)
Layer 1: Input Validator    — sanitise, detect injection, scope check
Layer 2: Permission Engine  — YAML spec enforcement, blocked/allowed/approval
Layer 3: Execution Sandbox  — Docker, no filesystem, allowlisted network only
Layer 4: Output Filter      — PII scrub, content policy, format enforcement
Layer 5: Audit Logger       — log BEFORE execute, immutable append-only

Every inbound task passes through all 5 layers to reach the agent.
Every outbound action passes back through all 5 layers before executing.

---

## Design principles — non-negotiable

1. **Fail safe** — unknown tool = BLOCKED, never allowed by default
2. **Log before execute** — audit entry written before action runs
3. **Layers are independent** — defeating one layer doesn't defeat others
4. **No secrets in prompts** — runtime injects via env vars only
5. **Worker cannot modify its own spec** — spec file is read-only to worker
6. **BLOCKED is hardcoded Python** — not in YAML, not overridable by prompt
7. **auto_approve_after_sla is always false** — humans approve, never timeout
8. **require_on_prem enforced at startup** — fails before any task runs
9. **Graceful degradation** — every connector has a free/local fallback
10. **Task state persisted before transition** — crash-safe, resumable
11. **ToolAccessRequest never auto-grants** — always requires manager approval
12. **Demo works with only ANTHROPIC_API_KEY** — everything else degrades gracefully

---

## Storage architecture — where everything lives

| Data | Storage | Why |
|------|---------|-----|
| Worker specs (YAML) | Git files | Infrastructure-as-code |
| Connector config | connectors.yaml (Git) | Same |
| Skill files | Git files | Same |
| Task state machine | Supabase Postgres | Crash recovery, queryable |
| Audit log | Supabase + local jsonl | Immutable, compliance |
| Approvals | Supabase Postgres | Manager dashboard |
| Tool access requests | Supabase Postgres | Approval workflow |
| Trust scores | Supabase Postgres | Updated per task |
| Episodic memory | Supabase Postgres | Past task summaries |
| Semantic memory (RAG) | Supabase pgvector | Embedded company knowledge |
| Working memory | Redis (TTL 1hr) | Fast, ephemeral |
| n8n job queue | Redis | Bull queue, crash-safe |

---

## Tech stack

| Component          | Technology                          |
|--------------------|-------------------------------------|
| Language           | Python 3.12                         |
| LLM — cloud        | Claude API (anthropic SDK)          |
| LLM — local        | Ollama (OpenAI-compatible API)      |
| LLM — self-hosted  | vLLM (any HuggingFace model)        |
| Workflow engine    | n8n queue mode (Redis + workers)    |
| Database           | Supabase (Postgres + pgvector)      |
| Cache / queue      | Redis                               |
| Containerisation   | Docker + docker-compose             |
| Dashboard (Phase 2)| Next.js + Tailwind + shadcn/ui      |
| Approval channel   | Slack (Block Kit) / stdout fallback |
| Web search         | Brave Search / DuckDuckGo fallback  |
| Web crawl          | Firecrawl / Crawl4AI fallback       |
| Voice STT          | Deepgram / Whisper local fallback   |
| Voice TTS          | ElevenLabs / OpenAI TTS fallback    |
| Meetings           | Recall.ai (Zoom + Teams + Meet)     |
| SMS                | Twilio / stdout fallback            |
| Secrets            | env vars / AWS Secrets Manager      |
| Package manager    | pip / requirements.txt              |

---

## Repository structure

```
openworker/
├── CLAUDE.md                      ← this file
├── CLAUDE_EXTENSION.md            ← architectural decisions v2
├── OVERNIGHT_SESSION.md           ← Phase 2 runtime build brief
├── README.md                      ← DONE
├── STATUS.md                      ← build tracker
├── docker-compose.yml             ← DONE — single command deploy
├── demo.py                        ← TO BUILD — Aryan demo script
├── .env.example                   ← all required env vars documented
├── connectors.yaml.example        ← TO BUILD — connector config template
├── requirements.txt
│
├── core/
│   ├── __init__.py
│   ├── agent_harness.py           ← DONE — 5-layer harness
│   ├── permission_engine.py       ← DONE — Layer 2
│   ├── model_resolver.py          ← DONE — LLM client factory
│   ├── input_validator.py         ← DONE — Layer 1 (23 tests)
│   ├── output_filter.py           ← DONE — Layer 4 (20 tests)
│   ├── audit_logger.py            ← DONE — Layer 5
│   ├── constants.py               ← DONE
│   ├── task_state.py              ← TO BUILD — TaskState + TaskRecord
│   ├── tool_access_request.py     ← TO BUILD — worker requests new tools
│   ├── trust_engine.py            ← TO BUILD — extract from permission_engine
│   └── worker_spec.py             ← TO BUILD — extract from permission_engine
│
├── runtime/
│   ├── __init__.py
│   ├── task_runner.py             ← TO BUILD (overnight session)
│   ├── approval_router.py         ← TO BUILD (overnight session)
│   ├── memory_manager.py          ← TO BUILD — 3 types, 3 backends
│   └── tool_executor.py           ← TO BUILD — uses ConnectorRegistry
│
├── connectors/                    ← TO BUILD — pluggable tool providers
│   ├── __init__.py
│   ├── base.py                    ← BaseConnector ABC + ConnectorResult
│   ├── registry.py                ← ConnectorRegistry
│   └── providers/
│       ├── web_search/
│       │   ├── brave.py
│       │   └── duckduckgo.py      ← free fallback, always works
│       ├── messaging/
│       │   └── slack_bot.py
│       └── documents/
│           └── local_file.py
│
├── database/
│   └── schema.sql                 ← TO BUILD — all Supabase tables
│
├── skills/
│   ├── base/
│   │   ├── email.md
│   │   ├── calendar.md
│   │   ├── messaging.md
│   │   ├── web_search.md
│   │   ├── policy_rag.md
│   │   └── escalation.md
│   └── roles/
│       ├── marketer.md
│       ├── developer.md
│       ├── tester.md
│       ├── code_reviewer.md
│       ├── idea_advisor.md        ← needed for Aryan demo
│       └── customer_support.md
│
├── workers/
│   ├── worker.maya.yaml           ← DONE
│   ├── worker.codera-qa.yaml      ← DONE
│   ├── worker.codera-review.yaml  ← DONE
│   ├── worker.aryan.yaml          ← DONE (first demo target)
│   └── worker.layla.yaml          ← DONE
│
├── spec/
│   └── WORKER_SPEC.md             ← DONE — open standard document
│
├── sandbox/
│   ├── Dockerfile.worker          ← DONE
│   ├── seccomp-profile.json       ← DONE
│   └── egress-proxy/
│       └── squid.conf             ← DONE
│
├── dashboard/                     ← Phase 2 — scaffold only
│
└── tests/
    ├── test_permission_engine.py  ← TO BUILD
    ├── test_input_validator.py    ← DONE (23 tests)
    ├── test_output_filter.py      ← DONE (20 tests)
    ├── test_harness.py            ← TO BUILD
    └── fixtures/
        └── worker.test.yaml       ← DONE
```

---

## What is built — v0.1 complete (43 tests passing)

### core/agent_harness.py — COMPLETE
- Full 5-layer async harness
- Mocked-LLM loop: approval pause, blocked, unknown, budget, happy path

### core/permission_engine.py — COMPLETE
- WorkerSpec, PermissionEngine, ApprovalRequest, AuditLogger
- Decision enum: ALLOWED / APPROVAL / BLOCKED / UNKNOWN
- ABSOLUTE_BLOCKS frozenset — hardcoded Python, not overridable
- Trust score nudging on approval/rejection

### core/model_resolver.py — COMPLETE
- ModelResolver, CostTracker, BudgetStatus, CostRecord
- roi_summary() — CFO-friendly AI vs human comparison
- 6 providers: anthropic, openai, ollama, vllm, openrouter, custom
- require_on_prem enforced at startup

### core/input_validator.py — COMPLETE (23 tests)
### core/output_filter.py — COMPLETE (20 tests)
### core/audit_logger.py — COMPLETE (extracted, re-exported)
### sandbox/ — COMPLETE (Docker, seccomp, squid)
### docker-compose.yml — COMPLETE (live: db, n8n, proxy verified)
### README.md — COMPLETE
### workers/*.yaml — ALL FIVE COMPLETE
### spec/WORKER_SPEC.md — COMPLETE

---

## What to build next — priority order

### IMMEDIATE: Aryan demo (see OVERNIGHT_SESSION.md)
Goal: `python demo.py "topic"` produces a research summary.
Requires: task_runner.py, tool_executor.py, approval_router.py, demo.py

### AFTER DEMO: Extension items (see CLAUDE_EXTENSION.md)
1. database/schema.sql
2. core/task_state.py
3. connectors/ module
4. Update tool_executor.py to use ConnectorRegistry
5. Update memory_manager.py — 3 memory types
6. core/tool_access_request.py
7. skills/ SKILL.md files

---

## Key data models — use these exact shapes

```python
@dataclass
class HarnessResult:
    success: bool
    worker_id: str
    task_id: str
    output: str | None
    approval_id: str | None
    error: str | None
    cost_record: CostRecord
    audit_ids: list[str]
    duration_seconds: float

@dataclass
class ValidationResult:
    ok: bool
    reason: str
    sanitised_task: str
    injection_detected: bool
    flags: list[str]

@dataclass
class FilterResult:
    ok: bool
    scrubbed_output: str
    pii_detected: bool
    content_flagged: bool
    flags: list[str]

class TaskState(str, Enum):
    CREATED            = "created"
    RUNNING            = "running"
    AWAITING_TOOL      = "awaiting_tool"
    AWAITING_APPROVAL  = "awaiting_approval"
    APPROVED           = "approved"
    REJECTED           = "rejected"
    COMPLETED          = "completed"
    FAILED             = "failed"
    SUSPENDED          = "suspended"

@dataclass
class ConnectorResult:
    success: bool
    output: str
    error: str | None = None
    metadata: dict | None = None

@dataclass
class ToolAccessRequest:
    request_id: str
    worker_id: str
    worker_name: str
    tools_requested: list[str]
    justification: str
    task_context: str
    suggested_tier: str
    status: str
    approved_tools: list[str]
    rejected_tools: list[str]
    manager_note: str | None
    created_at: str
    resolved_at: str | None
```

---

## System prompt construction — exact order

```
[1] Worker identity and role
[2] Autonomy and approval rules (include request_tool_access capability)
[3] Base skill context (skills/base/*.md)
[4] Role skill context (skills/roles/{pack}.md)
[5] Company knowledge — pgvector RAG retrieval on task query
[6] Working memory — last N episodic memories from Supabase
[7] Behavioural rules
[8] Task: {sanitised_task}
```

The spec is NOT in the system prompt.
The harness enforces permissions regardless of LLM decisions.

---

## Communication channels — all pluggable via ConnectorRegistry

| Tool name | Primary provider | Free fallback |
|-----------|-----------------|---------------|
| web_search | Brave Search | DuckDuckGo |
| web_crawl | Firecrawl | Crawl4AI (self-hosted) |
| email | Gmail API | SMTP |
| slack_post | Slack Bot | stdout [SLACK] |
| sms_send | Twilio | stdout [SMS] |
| voice_stt | Deepgram | Whisper local |
| voice_tts | ElevenLabs | OpenAI TTS |
| meetings | Recall.ai | stdout [MEETING] |

Meeting bots (Zoom/Teams/Meet) via Recall.ai:
- Bot joins as named AI participant ("Maya — AI")
- Listens for wake word ("Maya") — not always-on
- Speaks responses via TTS when addressed
- Produces post-meeting summary automatically
- Phase 3 — do not build yet

---

## ToolAccessRequest — worker requests new tools

Workers can request tools they don't have via request_tool_access tool.
This tool is always in the allowed tier for all workers.
It never auto-grants — always requires manager approval.
Routes through ApprovalRouter same as action approvals.
On approval: spec auto-updated, audit logged.

Example: Maya says "I need Canva and Meta Ads for this campaign."
Manager sees Slack request, approves Canva, rejects Meta Ads.
Maya's spec updated with canva_create in tools.allowed.

---

## What NOT to build until Phase 3+

- Voice/STT/TTS for Layla
- Meeting bots via Recall.ai
- Next.js dashboard (Phase 2 — brief in CLAUDE_DASHBOARD.md)
- Multi-agent coordination (L5 Lead workers)
- OpenRouter live testing
- Custom role pack builder UI
- SOC 2 / compliance tooling
- Desktop app (Electron/Tauri)

---

## Demo goal — definition of done for v0.1

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python demo.py "Summarise the competitive landscape for AI employee platforms"
```

Expected:
```
🤖 OpenWorker Demo
Worker: workers/worker.aryan.yaml
Task: Summarise the competitive landscape for AI employee platforms
──────────────────────────────────────────────
✅ Task completed
[executive summary output]

💰 Cost: $0.004231
📋 Audit: 8 entries written
```

This demo is shown to one human outside the household within 7 days.
Everything in this codebase serves that goal.

---

## Open source positioning

License: MIT
Domain: openworker.io
Tagline: "The open standard for employing AI inside organisations."
GitHub topics: ai-agents, llm, enterprise-ai, ai-governance,
               openworker, agent-framework, anthropic, python

This is NOT competing with CrewAI or LangGraph (agent builders).
This is the HR and governance layer that sits ON TOP of them.
Viktor/Nextiva = closed SaaS with no governance.
OpenWorker = open source, self-hosted, governed.

---

## Notes for Claude Code

- Read STATUS.md before every session to know current state
- WorkerSpec lives in permission_engine.py — do not redefine
- CostTracker lives in model_resolver.py — do not redefine
- Do not change agent_harness.py public API — 43 tests depend on it
- Use async/await throughout core/ and runtime/
- Type hints on everything
- Docstrings on every class and public method
- No print() — use logging module (except demo.py — CLI tool)
- No hardcoded strings — constants go in core/constants.py
- Every connector must have a graceful fallback
- Every new module needs tests before integration
