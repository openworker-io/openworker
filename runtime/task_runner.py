"""
OpenWorker Task Runner — runtime/task_runner.py
=================================================
The bridge between a trigger (CLI, n8n, webhook) and AgentHarness.run_task().
Loads a worker spec, wires the runtime/ layers (tool executor, approval
router, memory manager) into AgentHarness, runs one task, and prints a
clean status line — never raw Python objects.

Also persists a coarse TaskRecord (core/task_state.py) before and after
the run so a crash can at least be diagnosed from Supabase afterwards.
State writes are synchronous and best-effort: a failed write is logged
and the task continues — a database hiccup must never block execution
(docs/CLAUDE_EXTENSION.md constraint 11).

Note: this run's TaskRecord uses its own locally generated id (needed up
front, before AgentHarness has run, to write the CREATED/RUNNING rows).
AgentHarness.run_task() generates its own internal task_id for
HarnessResult/audit correlation — the two ids are intentionally
independent; full unification would require widening AgentHarness's
public API, which is out of scope here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.agent_harness import AgentHarness, HarnessResult
from core.permission_engine import WorkerSpec, WorkerStatus
from core.task_state import TaskRecord, TaskState, TaskStateStore
from runtime.approval_router import ApprovalRouter
from runtime.memory_manager import MemoryManager
from runtime.tool_executor import make_harness_executor

logger = logging.getLogger("openworker.task_runner")


class TaskRunner:
    """
    Usage:
        runner = TaskRunner("workers/worker.aryan.yaml")
        result = await runner.run("Summarise the AI employee market landscape")
        print(result.output)
    """

    def __init__(self, spec_path: str | Path | None = None):
        resolved = spec_path or os.environ.get("OPENWORKER_SPEC")
        if not resolved:
            raise ValueError(
                "TaskRunner needs a spec_path argument or the OPENWORKER_SPEC env var set."
            )
        self.spec_path = resolved
        self._run_id = str(uuid.uuid4())
        self._task_state = TaskStateStore()

        # tool_executor needs the spec + a task id for workspace isolation,
        # so the spec is loaded once here and passed into the real harness
        # via its public constructor params.
        spec = WorkerSpec(resolved)
        self.harness = AgentHarness(
            resolved,
            tool_executor=make_harness_executor(spec, task_id=self._run_id),
            memory_manager=MemoryManager(spec.worker_id),
            approval_router=ApprovalRouter(spec),
        )

    async def resume_check(self) -> list[TaskRecord]:
        """
        Surfaces tasks left RUNNING or AWAITING_TOOL for this worker —
        evidence of a crash. v0.1 does not auto-resume them (that needs a
        re-entrant agent loop); it logs them clearly so a human or a future
        TaskRunner version can act. AWAITING_APPROVAL tasks are excluded —
        those wait for a human signal by design, not a crash symptom.
        """
        incomplete = await self._task_state.get_incomplete_tasks(self.harness.spec.worker_id)
        for record in incomplete:
            logger.warning(
                "Found incomplete task %s for %s in state %s (likely a crash) — "
                "v0.1 does not auto-resume; review manually.",
                record.task_id, record.worker_name, record.state.value,
            )
        return incomplete

    async def run(self, task: str, context: dict | None = None) -> HarnessResult:
        """Run one task end-to-end. Pre-flight checks, then delegate to the harness."""
        spec = self.harness.spec

        if spec.worker_status != WorkerStatus.ACTIVE:
            logger.warning("Worker %s is %s — refusing task before harness runs", spec.worker_name, spec.worker_status.value)

        budget = self.harness.cost_tracker.check_budget()
        if not budget.ok:
            logger.warning("Budget check failed before task started: %s", budget.message)

        record = TaskRecord(
            task_id=self._run_id,
            worker_id=spec.worker_id,
            worker_name=spec.worker_name,
            task_input=task,
            state=TaskState.CREATED,
        )
        await self._task_state.upsert_task(record)

        record.state = TaskState.RUNNING
        await self._task_state.upsert_task(record)

        result = await self.harness.run_task(task, context or {})

        if result.approval_id:
            logger.info("Task %s paused for approval %s", result.task_id, result.approval_id)
            record.state = TaskState.AWAITING_APPROVAL
            record.approval_id = result.approval_id
        elif not result.success:
            logger.warning("Task %s failed: %s", result.task_id, result.error)
            record.state = TaskState.FAILED
            record.error = result.error
        else:
            logger.info("Task %s completed (%d audit entries)", result.task_id, len(result.audit_ids))
            record.state = TaskState.COMPLETED
            record.output = result.output

        record.cost_usd = result.cost_record.cost_usd
        if record.state in (TaskState.COMPLETED, TaskState.FAILED):
            record.completed_at = datetime.now(timezone.utc).isoformat()
        await self._task_state.upsert_task(record)

        self.harness.audit.log_event(
            "final_cost_record",
            spec.worker_id,
            {
                "task_id": result.task_id,
                "provider": result.cost_record.provider,
                "model": result.cost_record.model,
                "input_tokens": result.cost_record.input_tokens,
                "output_tokens": result.cost_record.output_tokens,
                "cost_usd": result.cost_record.cost_usd,
            },
        )

        return result

    def run_sync(self, task: str, context: dict | None = None) -> HarnessResult:
        """Synchronous wrapper around run() for CLI usage."""
        return asyncio.run(self.run(task, context))
