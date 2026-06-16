# OpenWorker

**Hire AI workers. Govern them like staff.**

OpenWorker is an open-source **AI Employment Framework** — the layer that lets an organisation *employ* AI agents the way it employs humans: hire, onboard, assign a role, grant permissions, supervise, evaluate, promote, suspend.

It exists because enterprises don't fear AI capability — they fear AI without governance. Frameworks like CrewAI and LangGraph answer *how to build* agents. OpenWorker answers *how to employ and govern* them.

Try it right now:

```bash
git clone https://github.com/openworker-io/openworker && cd openworker
cp .env.example .env        # add your ANTHROPIC_API_KEY
docker compose up -d
```

---

## The idea

Every AI worker is defined by a single YAML file — part job description, part permission manifest, part HR record:

```yaml
identity:
  name: Maya
  status: active            # active | suspended | onboarding | retired
org:
  department: Marketing
  reports_to: { name: Sarah Johnson, email: sarah.johnson@company.com }
autonomy:
  level: L2                 # L1 Intern → L5 Lead
  trust_score: 87           # drives promotion — always human-approved
tools:
  allowed:           [web_search, linkedin_draft, canva_create]
  approval_required: [linkedin_publish, email_send_external]
  blocked:           [payroll_systems, production_databases]
cost:
  monthly_budget_usd: 500
  per_task_budget_usd: 2.00
model:
  provider: anthropic       # or ollama / vllm for fully on-prem
  name: claude-sonnet-4-6
```

The spec is **enforced by the runtime, not by the prompt**. The LLM never sees its own permission boundaries — the harness applies them regardless of what the model decides. No instruction, injected or otherwise, can override a `blocked` entry.

## The five-layer harness

Every task in, and every action out, passes through all five layers:

| Layer | Module | Job |
|---|---|---|
| 1. Input Validator | `core/input_validator.py` | Sanitise, detect prompt injection, scope check |
| 2. Permission Engine | `core/permission_engine.py` | Enforce the YAML spec: allowed / approval / blocked |
| 3. Execution Sandbox | `sandbox/` | Read-only container, all caps dropped, seccomp allowlist, egress proxy |
| 4. Output Filter | `core/output_filter.py` | PII + secret scrubbing, content policy, format checks |
| 5. Audit Logger | `core/audit_logger.py` | Append-only trail, written **before** every action executes |

Fail-safe defaults throughout: an unknown tool is blocked, an approval never auto-expires into a yes, and a worker can never modify its own spec.

## Autonomy levels

| Level | Label | Can do |
|---|---|---|
| L1 | Intern | Observe and suggest only |
| L2 | Junior | Execute — every action needs human approval |
| L3 | Mid | Execute low-risk freely; high-risk needs approval |
| L4 | Senior | Execute most tasks; escalate exceptions |
| L5 | Lead | Coordinate and assign other workers |

A trust score (0–100) accumulates from approval outcomes. Promotion between levels is **always human-initiated** — the score informs the decision, it never makes it.

## Quickstart (Python, no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

```python
import asyncio
from core.agent_harness import AgentHarness

harness = AgentHarness("workers/worker.maya.yaml")
result = asyncio.run(harness.run_task(
    "Draft a LinkedIn post about our Q3 product launch"
))

print(result.output)              # filtered, PII-scrubbed final text
print(result.cost_record)        # tokens + USD for this task
print(result.approval_id)        # set if the task paused for sign-off
```

See the permission engine decide in isolation (no API key needed):

```bash
python -m core.permission_engine workers/worker.maya.yaml
```

Run the tests:

```bash
pytest
```

## Worker templates

Five ready-to-edit specs ship in [`workers/`](workers/):

| Worker | Role |
|---|---|
| **Maya** | Marketing assistant (reference implementation) |
| **Codera-QA** | Software tester |
| **Codera-Review** | Code reviewer |
| **Aryan** | Idea → plan advisor |
| **Layla** | Customer support (voice-capable, Phase 3) |

The full spec format is documented in [`spec/WORKER_SPEC.md`](spec/WORKER_SPEC.md).

## Status — v0.1

The v0.1 goal: **one worker, one task, one approval, full audit trail.**

Built and tested:
- ✅ Worker spec standard + five templates
- ✅ Permission engine (allowed / approval / blocked / unknown→blocked)
- ✅ Agent harness wiring all five layers (anthropic provider)
- ✅ Input validator and output filter, with test suites
- ✅ Hardened worker container + seccomp profile + egress allowlist
- ✅ Single-command deploy (db, n8n, egress proxy)

In progress:
- 🚧 `runtime/` — task runner, Slack approval router, Supabase memory, MCP tool executor
- 🚧 FastAPI control plane
- 🚧 Next.js manager dashboard (Phase 2)
- 🚧 Voice support for Layla, multi-worker coordination (Phase 3)

## What OpenWorker is not

It is **not another agent framework**. CrewAI, LangGraph, and friends are excellent at orchestrating agents — OpenWorker is the HR and governance layer that sits on top of whatever builds your agents. If your security team asks "what exactly can this thing do, who approved it, and where's the log?" — this is the answer.

## License

MIT
