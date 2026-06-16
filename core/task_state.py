"""
OpenWorker Task State — core/task_state.py
=============================================
Crash-recoverable task state machine, persisted to Supabase
(see database/schema.sql, table `tasks`).

State must be written BEFORE the corresponding transition executes
(constraint 11 in docs/CLAUDE_EXTENSION.md) — if the process dies
mid-tool-call, `current_tool` on the persisted row tells the next
TaskRunner what was in flight.

Falls back to logging only when SUPABASE_URL / SUPABASE_KEY aren't
configured or the `supabase` package isn't installed — task execution
must never block on a database write (constraint 11).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("openworker.task_state")

_TABLE = "tasks"


class TaskState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    AWAITING_TOOL = "awaiting_tool"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    SUSPENDED = "suspended"


@dataclass
class TaskRecord:
    """Mirrors one row of the `tasks` table."""
    task_id: str
    worker_id: str
    worker_name: str
    task_input: str
    state: TaskState
    current_tool: str | None = None
    approval_id: str | None = None
    output: str | None = None
    error: str | None = None
    cost_usd: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "worker_name": self.worker_name,
            "task_input": self.task_input,
            "state": self.state.value,
            "current_tool": self.current_tool,
            "approval_id": self.approval_id,
            "output": self.output,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TaskRecord":
        return cls(
            task_id=row["task_id"],
            worker_id=row["worker_id"],
            worker_name=row["worker_name"],
            task_input=row["task_input"],
            state=TaskState(row["state"]),
            current_tool=row.get("current_tool"),
            approval_id=row.get("approval_id"),
            output=row.get("output"),
            error=row.get("error"),
            cost_usd=float(row.get("cost_usd") or 0.0),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
            completed_at=row.get("completed_at"),
        )


class TaskStateStore:
    """
    Supabase-backed persistence for TaskRecord. Falls back to an
    in-process dict (and a log line) when Supabase isn't configured —
    crash recovery simply isn't available in that mode, but nothing
    blocks or raises.

    Usage:
        store = TaskStateStore()
        await store.upsert_task(record)
        record = await store.get_task(task_id)
        stuck = await store.get_incomplete_tasks(worker_id)
    """

    INCOMPLETE_STATES = (TaskState.RUNNING, TaskState.AWAITING_TOOL)

    def __init__(self):
        self.supabase_url = os.environ.get("SUPABASE_URL")
        self.supabase_key = os.environ.get("SUPABASE_KEY")
        self._client: Any | None = None
        self._in_memory: dict[str, TaskRecord] = {}

        if self.supabase_url and self.supabase_key:
            try:
                from supabase import create_client

                self._client = create_client(self.supabase_url, self.supabase_key)
            except ImportError:
                logger.warning("supabase package not installed — task state will not survive a crash")
            except Exception as exc:
                logger.warning("Could not initialise Supabase client: %s — task state is in-memory only", exc)

    async def upsert_task(self, record: TaskRecord) -> None:
        """Write the current state BEFORE executing the transition it represents."""
        record.updated_at = datetime.now(timezone.utc).isoformat()
        if self._client is not None:
            try:
                self._client.table(_TABLE).upsert(record.to_row()).execute()
                return
            except Exception as exc:
                logger.warning(
                    "Supabase task state write failed for %s: %s — continuing without crash recovery",
                    record.task_id, exc,
                )
        self._in_memory[record.task_id] = record

    async def get_task(self, task_id: str) -> TaskRecord | None:
        if self._client is not None:
            try:
                response = self._client.table(_TABLE).select("*").eq("task_id", task_id).limit(1).execute()
                rows = response.data or []
                return TaskRecord.from_row(rows[0]) if rows else None
            except Exception as exc:
                logger.warning("Supabase task lookup failed for %s: %s", task_id, exc)
                return None
        return self._in_memory.get(task_id)

    async def get_incomplete_tasks(self, worker_id: str) -> list[TaskRecord]:
        """
        Tasks left RUNNING or AWAITING_TOOL for this worker — candidates
        to resume on TaskRunner startup. AWAITING_APPROVAL tasks are
        intentionally excluded: they wait for a human signal, never auto-resume.
        """
        states = [s.value for s in self.INCOMPLETE_STATES]
        if self._client is not None:
            try:
                response = (
                    self._client.table(_TABLE)
                    .select("*")
                    .eq("worker_id", worker_id)
                    .in_("state", states)
                    .execute()
                )
                return [TaskRecord.from_row(r) for r in (response.data or [])]
            except Exception as exc:
                logger.warning("Supabase incomplete-task lookup failed for %s: %s", worker_id, exc)
                return []
        return [r for r in self._in_memory.values() if r.worker_id == worker_id and r.state in self.INCOMPLETE_STATES]
