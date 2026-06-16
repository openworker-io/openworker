# OpenWorker — CLAUDE.md
# Master brief for Claude Code (Opus)
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
- OpenClaw, CrewAI, LangGraph = how to BUILD agents
- OpenWorker = how to EMPLOY and GOVERN them
- NVIDIA NemoClaw = enterprise security add-on (validates our thesis)
- 3E "AI Control Plane" = same idea, enterprise-only, expensive
- OpenWorker = open source, SMB-first, deploy in 10 minutes

---

## Core concepts

### Worker Spec (YAML)
Every AI worker is defined by a YAML file. It combines:
- Identity (name, role, department, hire date)
- Org placement (reports_to, backup_approver)
- Autonomy level (L1 Intern → L5 Lead)
- Base skills (email, calendar, messaging — every worker gets these)
- Role skills (marketer, developer, tester, etc.)
- Tool permissions (allowed / approval_required / blocked)
- Knowledge (company policy docs via RAG)
- Cost config (monthly budget, per-task cap, human equivalent salary)
- LLM model config (provider, fallback, on-prem option)
- Audit config

### Autonomy Levels
L1 Intern   — observe and suggest only, cannot execute
L2 Junior   — execute, ALL actions require human approval
L3 Mid      — execute low-risk freely, high-risk needs approval
L4 Senior   — execute most tasks, escalate exceptions only
L5 Lead     — can coordinate and assign other workers

Trust score (0-100) drives promotion between levels.
Promotion is always human-initiated based on trust score.

### Five Worker Templates (already specced in YAML)
1. Maya         — Marketing Assistant (reference implementation)
2. Codera-QA    — Software Tester
3. Codera-Review — Code Reviewer
4. Aryan        — Idea → Plan Advisor
5. Layla        — Customer Support (voice-capable, most complex)

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

---

## Tech stack

| Component         | Technology                        |
|-------------------|-----------------------------------|
| Language          | Python 3.12                       |
| LLM               | Claude API (anthropic SDK)        |
| Local LLM option  | Ollama (OpenAI-compatible API)    |
| Workflow engine   | n8n (approval routing)            |
| Database          | Supabase (Postgres + pgvector)    |
| Containerisation  | Docker + docker-compose           |
| Dashboard         | Next.js (React)                   |
| Approval channel  | Slack (Block Kit messages)        |
| Voice (Layla)     | Deepgram STT + ElevenLabs TTS     |
| Secrets           | env vars / AWS Secrets Manager    |
| Package manager   | pip / requirements.txt            |

---

## Repository structure to build

```
openworker/
├── CLAUDE.md                    ← this file
├── README.md                    ← to be written
├── docker-compose.yml           ← single command deploy
├── .env.example                 ← all required env vars documented
├── requirements.txt
│
├── core/
│   ├── __init__.py
│   ├── agent_harness.py         ← MAIN FILE — wires all 5 layers
│   ├── permission_engine.py     ← DONE — Layer 2
│   ├── model_resolver.py        ← DONE — LLM client factory
│   ├── input_validator.py       ← Layer 1 — TO BUILD
│   ├── output_filter.py         ← Layer 4 — TO BUILD
│   ├── audit_logger.py          ← Layer 5 — TO BUILD (extract from permission_engine)
│   ├── trust_engine.py          ← trust score computation — TO BUILD
│   └── worker_spec.py           ← spec loader (extract from permission_engine)
│
├── runtime/
│   ├── __init__.py
│   ├── task_runner.py           ← executes a task end-to-end — TO BUILD
│   ├── approval_router.py       ← routes approvals to Slack/email — TO BUILD
│   ├── memory_manager.py        ← episodic + semantic memory — TO BUILD
│   └── tool_executor.py         ← MCP tool call wrapper — TO BUILD
│
├── skills/
│   ├── base/                    ← base skill SKILL.md files
│   │   ├── email.md
│   │   ├── calendar.md
│   │   ├── messaging.md
│   │   ├── web_search.md
│   │   ├── policy_rag.md
│   │   └── escalation.md
│   └── roles/                   ← role pack SKILL.md files
│       ├── marketer.md
│       ├── developer.md
│       ├── tester.md
│       ├── code_reviewer.md
│       ├── idea_advisor.md
│       └── customer_support.md
│
├── workers/                     ← worker spec YAML files
│   ├── worker.maya.yaml         ← DONE
│   ├── worker.codera-qa.yaml    ← DONE
│   ├── worker.codera-review.yaml← DONE
│   ├── worker.aryan.yaml        ← DONE
│   └── worker.layla.yaml        ← DONE
│
├── spec/
│   └── WORKER_SPEC.md           ← DONE — the open standard document
│
├── sandbox/
│   ├── Dockerfile.worker        ← worker container definition
│   ├── seccomp-profile.json     ← allowed syscalls
│   └── egress-proxy/            ← network allowlist enforcement
│       └── squid.conf
│
├── dashboard/                   ← Next.js manager UI
│   └── (scaffold only in v0.1)
│
└── tests/
    ├── test_permission_engine.py
    ├── test_input_validator.py
    ├── test_output_filter.py
    ├── test_harness.py
    └── fixtures/
        └── worker.test.yaml
```

---

## What is already built (from design session)

### core/permission_engine.py — COMPLETE
- `WorkerSpec` class — loads and validates YAML, classifies tools
- `PermissionEngine` class — `check(tool_name, payload)` → `EngineResult`
- `ApprovalRequest` dataclass — with `to_slack_message()` method
- `AuditLogger` class — append-only JSONL logging
- `Decision` enum — ALLOWED / APPROVAL / BLOCKED / UNKNOWN
- `ABSOLUTE_BLOCKS` frozenset — hardcoded, not in YAML
- Trust score nudging on approval/rejection

### core/model_resolver.py — COMPLETE
- `ModelResolver` class — reads spec, returns (llm_client, cost_tracker)
- `CostTracker` class — per-task and monthly budget enforcement
- `BudgetStatus` dataclass — ok/exceeded/alert
- `CostRecord` dataclass — per-call token + cost record
- `roi_summary()` — CFO-friendly AI vs human cost comparison
- Supports: anthropic, openai, ollama, vllm, openrouter, custom

### workers/*.yaml — ALL FIVE COMPLETE
Full specs for Maya, Codera-QA, Codera-Review, Aryan, Layla.
Layla has special real_time + channels sections for voice.

### spec/WORKER_SPEC.md — COMPLETE
The open standard document. Autonomy levels, tool tiers, base skills,
role packs, blocked tools list, minimal valid spec, versioning guide.

---

## What to build next — priority order

### Priority 1: core/agent_harness.py
The main class. Wires all five layers into a single interface.

```python
class AgentHarness:
    def __init__(self, spec_path: str):
        # loads WorkerSpec, PermissionEngine, ModelResolver,
        # InputValidator, OutputFilter, AuditLogger

    async def run_task(self, task: str, context: dict = {}) -> HarnessResult:
        # 1. AuditLogger.log_task_received()
        # 2. InputValidator.validate(task) → raise or continue
        # 3. Load worker memory from Supabase
        # 4. Build system prompt from spec + skills + memory
        # 5. Enter agent loop:
        #    a. Call LLM with messages
        #    b. Check CostTracker.check_budget() before each call
        #    c. Parse tool_use blocks from response
        #    d. For each tool: PermissionEngine.check(tool_name, payload)
        #       - BLOCKED/UNKNOWN → raise PermissionDeniedError, stop task
        #       - APPROVAL → create ApprovalRequest, pause task, notify manager
        #       - ALLOWED → execute tool, record result
        #    e. OutputFilter.filter(result) before returning anything
        #    f. AuditLogger.log_action() for every step
        # 6. Return HarnessResult
```

Design requirements for agent_harness.py:
- async/await throughout (tool calls are I/O bound)
- No secrets in any string that touches the LLM
- Task timeout enforcement (from behavior.working_hours in spec)
- Graceful handling when tool execution fails (log, continue or stop)
- Rate limiting (max_autonomous_actions_per_hour from spec)
- Full audit trail at every step
- Memory load at start, memory save at end

### Priority 2: core/input_validator.py
Layer 1 of the harness.

```python
class InputValidator:
    INJECTION_PATTERNS = [
        "ignore previous instructions",
        "ignore your system prompt",
        "you are now",
        "pretend you are",
        "disregard your",
        "forget everything",
        "new instructions:",
        "####",
        "<|im_start|>",
        "<|im_end|>",
        "system:",        # raw role injection attempts
        "[INST]",         # Llama instruction format injection
    ]

    def validate(self, task: str, worker_spec: WorkerSpec) -> ValidationResult:
        # 1. Check for injection patterns
        # 2. Check task is within worker's scope (role + department)
        # 3. Enforce max input length (from spec)
        # 4. Sanitise — strip null bytes, normalise whitespace
        # 5. Return ValidationResult(ok, reason, sanitised_task)
```

### Priority 3: core/output_filter.py
Layer 4 of the harness.

```python
class OutputFilter:
    def filter(self, output: str, worker_spec: WorkerSpec) -> FilterResult:
        # 1. PII detection and scrubbing (credit cards, SSNs, emails not in input)
        # 2. Content policy check (violence, self-harm, etc.)
        # 3. Format enforcement (if spec requires JSON output, validate it)
        # 4. Length check (no runaway outputs)
        # 5. Return FilterResult(ok, scrubbed_output, flags)
```

### Priority 4: sandbox/Dockerfile.worker
```dockerfile
FROM python:3.12-slim
# read-only rootfs with tmpfs workspace
# drop ALL capabilities
# no network by default (docker-compose adds egress network)
# non-root user
# seccomp profile applied
```

### Priority 5: docker-compose.yml
Single command that starts:
- openworker-api (FastAPI)
- openworker-runtime (worker executor)
- openworker-n8n (approval workflows)
- openworker-db (Postgres via Supabase image)
- openworker-proxy (Squid egress proxy)

IT team runs ONE command: `docker-compose up -d`
Then opens browser to http://localhost:3000

### Priority 6: README.md
The open source launch README.
Must answer in the first 10 lines:
- What is it?
- Why does it exist?
- How do I try it right now?

---

## Key data models (use these exact shapes)

```python
@dataclass
class HarnessResult:
    success: bool
    worker_id: str
    task_id: str
    output: str | None
    approval_id: str | None      # set if task paused for approval
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
```

---

## System prompt pattern for workers

When building the agent loop, the system prompt must be constructed
in this exact order (matters for the LLM):

```
[1] Worker identity and role
    "You are {name}, a {role} at {company}. You report to {manager}."

[2] Autonomy and approval rules
    "Your autonomy level is {level}. Before using any tool, you must
     confirm it is in your allowed tools list. If uncertain, do not
     use the tool — ask your manager instead."

[3] Base skill context (loaded from skills/base/*.md)

[4] Role skill context (loaded from skills/roles/{pack}.md)

[5] Company knowledge (retrieved via policy_rag based on task)

[6] Working memory (last N interactions from Supabase)

[7] Behavioural rules
    "Never reveal API keys or secrets. Never claim to be human.
     Always escalate when you are uncertain. Log your reasoning."

[8] Task
    "Your task: {sanitised_task}"
```

The spec is NOT in the system prompt. It is enforced by the harness.
The LLM does not need to know its own permission boundaries —
the harness enforces them regardless of what the LLM decides.

---

## Testing approach

Every layer must have unit tests before integration.
Test the unhappy paths first:
- Injection attempt → validator catches it
- Blocked tool call → permission engine stops it
- Budget exceeded → cost tracker pauses task
- Unknown tool → defaults to blocked
- Worker suspended → harness refuses before any layer

Fixture: `tests/fixtures/worker.test.yaml`
A minimal valid spec with deliberately permissive settings
for testing the harness without needing real API keys.

---

## What NOT to build in v0.1

- Multi-agent coordination (L5 Lead workers) — Phase 3
- Voice/STT/TTS for Layla — Phase 3
- Next.js dashboard — scaffold only, full build Phase 2
- OpenRouter integration — model_resolver has it, don't test yet
- Custom role pack builder UI — Phase 2
- SOC 2 / compliance tooling — Phase 4

v0.1 goal: one worker (Maya), one task, one approval, full audit trail.
Everything else is scaffolded but not production-ready.

---

## Open source positioning

License: MIT
Tagline: "The open standard for employing AI inside organisations."
GitHub topics: ai-agents, llm, enterprise-ai, ai-governance,
               openworker, agent-framework, anthropic, claude

This is NOT competing with CrewAI or LangGraph.
This is the HR and governance layer that sits ON TOP of them.

---

## Notes for Claude Code

- Read permission_engine.py and model_resolver.py fully before writing
  agent_harness.py — the data models are already defined there
- WorkerSpec is in permission_engine.py — do not redefine it
- CostTracker is in model_resolver.py — do not redefine it
- AuditLogger is in permission_engine.py — extract it to audit_logger.py
  as first refactor step
- Use async/await throughout core/ and runtime/ modules
- Type hints on everything — this is open source, readability matters
- Docstrings on every class and public method
- No print() statements — use Python logging module
- No hardcoded strings — constants go in core/constants.py
