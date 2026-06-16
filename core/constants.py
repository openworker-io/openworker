"""
OpenWorker Constants v0.1
=========================
Shared constants for the core/ and runtime/ modules.
No hardcoded strings or magic numbers anywhere else — they live here.
"""

from __future__ import annotations

# ─────────────────────────────────────────────
# AGENT LOOP
# ─────────────────────────────────────────────

# Hard cap on LLM round-trips inside a single task.
# Prevents runaway loops even if budget checks somehow pass.
MAX_AGENT_ITERATIONS: int = 25

# Default wall-clock timeout for one task (seconds).
DEFAULT_TASK_TIMEOUT_SECONDS: float = 600.0

# Default max_tokens for a single LLM response.
DEFAULT_MAX_OUTPUT_TOKENS: int = 16000

# Fallback rate limit when behavior.max_autonomous_actions_per_hour
# is missing from the worker spec.
DEFAULT_MAX_ACTIONS_PER_HOUR: int = 10

# Fallback max input length (characters) for inbound tasks.
DEFAULT_MAX_INPUT_CHARS: int = 20_000

# Max output length (characters) — guards against runaway outputs.
DEFAULT_MAX_OUTPUT_CHARS: int = 50_000


# ─────────────────────────────────────────────
# AUDIT EVENT TYPES
# ─────────────────────────────────────────────

EVENT_TASK_RECEIVED = "task_received"
EVENT_TASK_VALIDATED = "task_validated"
EVENT_TASK_REJECTED = "task_rejected"
EVENT_LLM_CALL = "llm_call"
EVENT_TOOL_EXECUTED = "tool_executed"
EVENT_TOOL_FAILED = "tool_failed"
EVENT_TASK_PAUSED_FOR_APPROVAL = "task_paused_for_approval"
EVENT_TASK_COMPLETED = "task_completed"
EVENT_TASK_FAILED = "task_failed"
EVENT_TOOL_ACCESS_REQUESTED = "tool_access_requested"


# ─────────────────────────────────────────────
# HARNESS REASONS (machine-readable error/refusal codes)
# ─────────────────────────────────────────────

REASON_WORKER_NOT_ACTIVE = "worker_not_active"
REASON_OUTSIDE_WORKING_HOURS = "outside_working_hours"
REASON_INPUT_REJECTED = "input_rejected"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_RATE_LIMITED = "rate_limit_exceeded"
REASON_PERMISSION_DENIED = "permission_denied"
REASON_TIMEOUT = "task_timeout"
REASON_MAX_ITERATIONS = "max_iterations_reached"
REASON_OUTPUT_BLOCKED = "output_blocked_by_filter"
REASON_LLM_ERROR = "llm_error"


# ─────────────────────────────────────────────
# SKILL FILE LOCATIONS (relative to repo root)
# ─────────────────────────────────────────────

SKILLS_BASE_DIR = "skills/base"
SKILLS_ROLES_DIR = "skills/roles"


# ─────────────────────────────────────────────
# TOOL ACCESS REQUESTS
# ─────────────────────────────────────────────

# Always available to every worker regardless of spec — it's a
# communication tool (asks a manager for new tools), not an action
# tool, and it never auto-grants anything.
REQUEST_TOOL_ACCESS_TOOL = "request_tool_access"
