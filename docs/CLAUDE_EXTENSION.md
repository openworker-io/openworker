# OpenWorker — CLAUDE_EXTENSION.md
# Extension to OVERNIGHT_SESSION.md
# Date: 2026-06-16 (late session)
# Status: Claude Code is already running OVERNIGHT_SESSION.md
#         Do NOT interrupt current work.
#         Pick up these items after OVERNIGHT_SESSION.md is complete.
#         Read CLAUDE.md + OVERNIGHT_SESSION.md + STATUS.md first.

---

## CONTEXT: ARCHITECTURAL DECISIONS MADE TONIGHT

Five critical architectural questions were resolved after the overnight
session brief was written. These change the design of several modules
already in progress. Read this before finalising any runtime/ module.

---

## DECISION 1: Storage split — files vs database

**Rule (non-negotiable):**

| What | Where | Why |
|------|-------|-----|
| Worker specs (YAML) | Git files | Infrastructure-as-code, auditable, PR-reviewable |
| Connector config | Git files | Same — permissions are code |
| Skill files (SKILL.md) | Git files | Same |
| Task state | Supabase (Postgres) | Must survive crashes, queryable |
| Audit log | Supabase (Postgres) | Immutable, queryable, compliance |
| Approvals | Supabase (Postgres) | State machine, manager dashboard |
| Tool access requests | Supabase (Postgres) | Approval workflow |
| Trust scores | Supabase (Postgres) | Updated after every task |
| Episodic memory | Supabase (Postgres) | Past task summaries per worker |
| Semantic memory / RAG | Supabase pgvector | Embedded company knowledge |
| Working memory | Redis | Fast, ephemeral, TTL 1 hour |
| n8n job queue | Redis | Already using Bull queue |

**Impact on runtime/ modules being built tonight:**
- `memory_manager.py` must write to Supabase, not in-memory dict
- `task_runner.py` must persist task state to Supabase at every transition
- `audit_logger.py` should write to BOTH jsonl (local, fast) AND Supabase
  (persistent, queryable). Local jsonl is the immediate write, Supabase
  is the async backup. If Supabase is not configured, jsonl only is fine.

---

## DECISION 2: Task state machine — crash recovery

Every task must be recoverable after a crash or restart.
Implement as a state machine persisted to Supabase.

```python
class TaskState(str, Enum):
    CREATED            = "created"
    RUNNING            = "running"
    AWAITING_TOOL      = "awaiting_tool"        # executing a tool
    AWAITING_APPROVAL  = "awaiting_approval"    # paused for human
    APPROVED           = "approved"             # manager approved, resuming
    REJECTED           = "rejected"             # manager rejected
    COMPLETED          = "completed"
    FAILED             = "failed"               # error or crash
    SUSPENDED          = "suspended"            # worker suspended mid-task

@dataclass
class TaskRecord:
    task_id: str
    worker_id: str
    worker_name: str
    task_input: str
    state: TaskState
    current_tool: str | None        # which tool is running when crashed
    approval_id: str | None
    output: str | None
    error: str | None
    cost_usd: float
    created_at: str
    updated_at: str
    completed_at: str | None
```

**State transition rules:**
- Write new state to Supabase BEFORE executing the transition
- On TaskRunner startup: query Supabase for RUNNING or AWAITING_TOOL
  tasks for this worker — resume them
- AWAITING_APPROVAL tasks: do not auto-resume — wait for manager signal
- FAILED tasks: log and notify manager, do not auto-retry

**New file to create:**
`core/task_state.py` — TaskState enum + TaskRecord dataclass + 
Supabase persistence methods (upsert_task, get_task, get_incomplete_tasks)

---

## DECISION 3: Connector registry — pluggable tool providers

**The problem with current tool_executor.py approach:**
Hardcoded provider logic (if brave_key: use brave, elif serpapi: use serpapi)
doesn't scale. Each new tool integration pollutes tool_executor.py.

**The solution: connector registry pattern**

New files to create:

```
connectors/
├── __init__.py
├── base.py              ← BaseConnector ABC + ConnectorResult
├── registry.py          ← ConnectorRegistry class
├── providers/
│   ├── web_search/
│   │   ├── brave.py     ← BraveSearchConnector
│   │   └── duckduckgo.py ← DuckDuckGoConnector (free fallback)
│   ├── messaging/
│   │   └── slack_bot.py  ← SlackConnector
│   └── documents/
│       └── local_file.py ← LocalFileConnector
```

`connectors/base.py`:
```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ConnectorResult:
    success: bool
    output: str
    error: str | None = None
    metadata: dict | None = None

class BaseConnector(ABC):
    @abstractmethod
    async def execute(self, payload: dict) -> ConnectorResult:
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """True if required env vars are set."""
        pass

    @abstractmethod
    def fallback_message(self) -> str:
        """Human-readable message when not configured."""
        pass
```

`connectors/registry.py`:
```python
class ConnectorRegistry:
    """
    Reads connectors.yaml and resolves tool_name → connector instance.
    Falls back to free/local provider if primary is not configured.
    """
    def get_connector(self, tool_name: str) -> BaseConnector:
        # read connectors.yaml
        # find provider for tool_name
        # instantiate the right connector class
        # if primary is_configured(): return primary
        # if fallback exists and is_configured(): return fallback
        # return NullConnector (logs clearly that tool is not configured)
```

`connectors.yaml` (company drops this in, never committed to Git):
```yaml
# connectors.yaml — company-specific credentials config
# Add your API keys to .env, reference the env var names here

connectors:
  web_search:
    provider: brave
    api_key_env: BRAVE_API_KEY
    fallback: duckduckgo        # free, no key needed

  web_crawl:
    provider: firecrawl
    api_key_env: FIRECRAWL_KEY
    fallback: crawl4ai          # self-hosted

  sms_send:
    provider: twilio
    account_sid_env: TWILIO_ACCOUNT_SID
    auth_token_env: TWILIO_AUTH_TOKEN
    from_number_env: TWILIO_FROM_NUMBER

  voice_stt:
    provider: deepgram
    api_key_env: DEEPGRAM_API_KEY
    fallback: whisper_local

  voice_tts:
    provider: elevenlabs
    api_key_env: ELEVENLABS_API_KEY
    fallback: openai_tts

  email:
    provider: gmail
    credentials_env: GMAIL_CREDENTIALS_JSON
    fallback: smtp

  slack:
    provider: slack_bot
    bot_token_env: SLACK_BOT_TOKEN
    signing_secret_env: SLACK_SIGNING_SECRET
    fallback: stdout            # prints to terminal if Slack not configured
```

**Update tool_executor.py to use registry:**
```python
class ToolExecutor:
    def __init__(self, registry: ConnectorRegistry):
        self.registry = registry

    async def execute(self, tool_name: str, payload: dict, spec: WorkerSpec) -> ToolResult:
        connector = self.registry.get_connector(tool_name)
        if not connector.is_configured():
            # log clearly, return graceful fallback
            return ToolResult(success=False,
                            error=connector.fallback_message(),
                            output="")
        result = await connector.execute(payload)
        return ToolResult(success=result.success,
                         output=result.output,
                         error=result.error)
```

---

## DECISION 4: Three memory types — different backends

`memory_manager.py` must support three distinct memory types:

```python
class MemoryType(str, Enum):
    EPISODIC  = "episodic"    # past task summaries — Supabase
    SEMANTIC  = "semantic"    # company knowledge RAG — pgvector
    WORKING   = "working"     # current task context — Redis, TTL 1hr

class MemoryManager:
    """
    Unified interface for all three memory types.
    Each type can have a different backend.
    All three are configured in connectors.yaml under memory:.
    """
    async def load_episodic(self, worker_id: str, limit: int = 10) -> list[dict]:
        # fetch last N task summaries for this worker from Supabase
        # return as list of {"role": "assistant", "content": "..."} dicts

    async def load_semantic(self, query: str, org_id: str, limit: int = 5) -> list[str]:
        # embed the query
        # pgvector similarity search in knowledge_base table
        # return top-K relevant chunks as strings
        # these get injected into system prompt section [5]

    async def load_working(self, task_id: str) -> dict:
        # fetch current task context from Redis by task_id
        # returns {} if not found (new task)

    async def save_episodic(self, worker_id: str, task: str, result: HarnessResult):
        # compress task + result into a summary
        # insert into Supabase worker_memory table

    async def save_working(self, task_id: str, context: dict, ttl: int = 3600):
        # store current task context in Redis with TTL

    async def ingest_knowledge(self, org_id: str, doc_path: str, worker_id: str = None):
        # chunk the document
        # embed each chunk (provider from connectors.yaml)
        # upsert into Supabase pgvector knowledge_base table
        # worker_id=None means available to all workers in org
```

**Supabase tables needed:**

```sql
-- Task state machine
CREATE TABLE tasks (
    task_id UUID PRIMARY KEY,
    worker_id TEXT NOT NULL,
    worker_name TEXT NOT NULL,
    task_input TEXT NOT NULL,
    state TEXT NOT NULL,
    current_tool TEXT,
    approval_id UUID,
    output TEXT,
    error TEXT,
    cost_usd DECIMAL(10,6),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Audit log (append-only — no updates, no deletes)
CREATE TABLE audit_log (
    audit_id UUID PRIMARY KEY,
    task_id UUID,
    worker_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tool_name TEXT,
    decision TEXT,
    reason TEXT,
    payload_hash TEXT,
    cost_usd DECIMAL(10,6),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Episodic memory
CREATE TABLE worker_memory (
    memory_id UUID PRIMARY KEY,
    worker_id TEXT NOT NULL,
    task_id UUID,
    summary TEXT NOT NULL,
    embedding vector(1536),    -- for semantic search on memories
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Knowledge base (RAG)
CREATE TABLE knowledge_base (
    chunk_id UUID PRIMARY KEY,
    org_id TEXT NOT NULL,
    worker_id TEXT,            -- NULL = available to all workers
    doc_name TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON knowledge_base USING ivfflat (embedding vector_cosine_ops);

-- Tool access requests (new feature)
CREATE TABLE tool_access_requests (
    request_id UUID PRIMARY KEY,
    worker_id TEXT NOT NULL,
    worker_name TEXT NOT NULL,
    tools_requested TEXT[] NOT NULL,
    justification TEXT NOT NULL,
    task_context TEXT,
    suggested_tier TEXT,       -- allowed | approval_required
    status TEXT DEFAULT 'pending',
    approved_tools TEXT[],
    rejected_tools TEXT[],
    manager_note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);
```

Create `database/schema.sql` with all these tables.
The docker-compose.yml should auto-run this on first startup.

---

## DECISION 5: ToolAccessRequest — worker requests new tools

This is a new feature. Workers can request access to tools they don't
have but believe would improve their output. Routes through the same
approval flow as action approvals.

**New file: `core/tool_access_request.py`**

```python
@dataclass
class ToolAccessRequest:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    worker_id: str = ""
    worker_name: str = ""
    tools_requested: list[str] = field(default_factory=list)
    justification: str = ""        # worker's reasoning
    task_context: str = ""         # what it was trying to do
    suggested_tier: str = "approval_required"
    status: str = "pending"
    approved_tools: list[str] = field(default_factory=list)
    rejected_tools: list[str] = field(default_factory=list)
    manager_note: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: str | None = None

    def to_slack_message(self) -> dict:
        """Formats as Slack Block Kit message for manager review."""
        return {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text",
                             "text": f"{self.worker_name} is requesting new tool access"}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"*Tools requested:*\n" +
                                     "\n".join(f"• `{t}`" for t in self.tools_requested)}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"*Why:*\n{self.justification}"}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"*Context:*\n{self.task_context}"}
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve All"},
                            "style": "primary",
                            "value": f"approve_tools:{self.request_id}"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Review Each"},
                            "value": f"review_tools:{self.request_id}"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Reject All"},
                            "style": "danger",
                            "value": f"reject_tools:{self.request_id}"
                        }
                    ]
                }
            ]
        }
```

**How the worker triggers this:**

In the agent loop, when the LLM indicates it wants a tool not in its
allowed list (rather than just being blocked silently), the harness
gives the LLM a special tool definition:

```python
# In agent_harness.py system prompt section [2], add:
"""
If you believe a tool you don't currently have access to would
significantly improve your output for this task, you may call
request_tool_access with:
  - tools: list of tool names you're requesting
  - justification: why these tools would help
  - suggested_tier: "allowed" or "approval_required"
This creates a request for your manager to review.
You should continue the task with what you have while waiting.
"""
```

The `request_tool_access` tool is always in the allowed tier
for all workers — it's a communication tool, not an action tool.
It never auto-grants access. It always requires manager approval.

---

## UPDATED REPO STRUCTURE

Add these new paths to the structure in CLAUDE.md:

```
openworker/
│
├── connectors/                  ← NEW — pluggable tool providers
│   ├── __init__.py
│   ├── base.py                  ← BaseConnector ABC + ConnectorResult
│   ├── registry.py              ← ConnectorRegistry
│   └── providers/
│       ├── web_search/
│       │   ├── brave.py
│       │   └── duckduckgo.py    ← free fallback
│       ├── messaging/
│       │   └── slack_bot.py
│       └── documents/
│           └── local_file.py
│
├── connectors.yaml.example      ← NEW — template, companies copy + fill
│
├── database/                    ← NEW — database schema
│   └── schema.sql               ← all Supabase tables
│
├── core/
│   ├── task_state.py            ← NEW — TaskState enum + TaskRecord
│   └── tool_access_request.py   ← NEW — worker requests new tools
│
└── runtime/
    └── memory_manager.py        ← UPDATE — three memory types + backends
```

---

## BUILD ORDER FOR THIS EXTENSION

After OVERNIGHT_SESSION.md items are complete, build in this order:

1. `database/schema.sql` — create all tables first, nothing else works without them
2. `core/task_state.py` — TaskState + TaskRecord + Supabase persistence
3. `connectors/base.py` + `connectors/registry.py` — connector interface
4. `connectors/providers/web_search/brave.py` + `duckduckgo.py` — first real connectors
5. `connectors/providers/messaging/slack_bot.py` — Slack connector
6. Update `runtime/tool_executor.py` to use ConnectorRegistry
7. Update `runtime/memory_manager.py` for three memory types
8. `core/tool_access_request.py` — ToolAccessRequest + Slack message
9. Update `runtime/approval_router.py` to handle tool access requests
10. Update `runtime/task_runner.py` to persist state at every transition
11. `connectors.yaml.example` — document all available connectors
12. Add Supabase tables to `docker-compose.yml` auto-init

---

## CONSTRAINTS — same as CLAUDE.md, plus:

9. **Graceful degradation is mandatory for every connector.**
   If BRAVE_API_KEY is not set, fall back to DuckDuckGo silently.
   If SLACK_BOT_TOKEN is not set, print to stdout with [SLACK] prefix.
   If SUPABASE_URL is not set, fall back to local jsonl + in-memory.
   The demo must work with ONLY ANTHROPIC_API_KEY set.

10. **ToolAccessRequest never auto-grants.**
    A worker calling request_tool_access creates a pending request.
    Nothing changes in the spec until a manager explicitly approves.
    The worker continues its task with existing tools while waiting.

11. **Task state writes are synchronous.**
    State must be written to Supabase before the transition executes.
    If Supabase write fails — log the error, continue with jsonl only.
    Never block task execution on a database write.

12. **Connectors are stateless.**
    Each connector is instantiated fresh per tool call.
    No connector should store credentials as instance variables
    beyond what's needed for one execute() call.
    Always read from os.environ — never cache credentials.

---

## DEFINITION OF DONE FOR THIS EXTENSION

```bash
# With only ANTHROPIC_API_KEY set — full graceful degradation:
python demo.py "Summarise the AI employee market"
# → web search uses DuckDuckGo (no key needed)
# → approval printed to stdout (no Slack needed)  
# → state saved to jsonl (no Supabase needed)
# → works completely

# With all keys set — full production path:
export ANTHROPIC_API_KEY=...
export BRAVE_API_KEY=...
export SLACK_BOT_TOKEN=...
export SUPABASE_URL=...
export SUPABASE_KEY=...
python demo.py "Summarise the AI employee market"
# → web search uses Brave
# → approval sent to Slack with buttons
# → state persisted to Supabase
# → resumable if crashed
```

Update STATUS.md when done.
