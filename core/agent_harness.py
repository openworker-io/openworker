"""
OpenWorker Agent Harness v0.1
=============================
The main entry point of the runtime. Wires all five harness layers
around a single LLM agent loop:

  Layer 1  InputValidator    — sanitise, detect injection, scope check
  Layer 2  PermissionEngine  — YAML spec enforcement (allowed/approval/blocked)
  Layer 3  Execution Sandbox — Docker (outside this process — see sandbox/)
  Layer 4  OutputFilter      — PII scrub, content policy, format enforcement
  Layer 5  AuditLogger       — log BEFORE execute, append-only

Every inbound task passes through all layers before reaching the agent.
Every outbound action passes back through them before executing.

The worker spec is NOT placed in the system prompt. The harness enforces
permissions regardless of what the LLM decides — no prompt can override
a BLOCKED decision.

Usage:
    harness = AgentHarness("workers/worker.maya.yaml")
    result = await harness.run_task("Draft a LinkedIn post about our Q3 launch")

v0.1 notes:
- Only provider 'anthropic' is supported by the agent loop. Other
  providers resolve a client (via ModelResolver) but the loop raises
  until the OpenAI-compatible path is built.
- InputValidator (Layer 1) and OutputFilter (Layer 4) are loaded from
  core.input_validator / core.output_filter when those modules exist.
  Until then, conservative built-in fallbacks are used so the harness
  is functional during the v0.1 build-out.
- Memory load/save are stubs until runtime/memory_manager.py lands.
- Approval notification is a stub until runtime/approval_router.py lands.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from core.constants import (
    DEFAULT_MAX_ACTIONS_PER_HOUR,
    DEFAULT_MAX_INPUT_CHARS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TASK_TIMEOUT_SECONDS,
    EVENT_LLM_CALL,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_PAUSED_FOR_APPROVAL,
    EVENT_TASK_RECEIVED,
    EVENT_TASK_REJECTED,
    EVENT_TASK_VALIDATED,
    EVENT_TOOL_EXECUTED,
    EVENT_TOOL_FAILED,
    MAX_AGENT_ITERATIONS,
    REASON_BUDGET_EXCEEDED,
    REASON_INPUT_REJECTED,
    REASON_LLM_ERROR,
    REASON_MAX_ITERATIONS,
    REASON_OUTSIDE_WORKING_HOURS,
    REASON_OUTPUT_BLOCKED,
    REASON_PERMISSION_DENIED,
    REASON_RATE_LIMITED,
    REASON_TIMEOUT,
    REASON_WORKER_NOT_ACTIVE,
    SKILLS_BASE_DIR,
    SKILLS_ROLES_DIR,
)
from core.audit_logger import AuditLogger
from core.model_resolver import CostRecord, CostTracker, ModelResolver
from core.permission_engine import PermissionEngine, WorkerSpec, WorkerStatus

logger = logging.getLogger("openworker.harness")


# ─────────────────────────────────────────────
# RESULT TYPE
# ─────────────────────────────────────────────

@dataclass
class HarnessResult:
    """
    Returned by AgentHarness.run_task() for every task — success, failure,
    or pause-for-approval. The runtime reads this and decides what to do next.
    """
    success: bool
    worker_id: str
    task_id: str
    output: str | None
    approval_id: str | None      # set if task paused for approval
    error: str | None
    cost_record: CostRecord
    audit_ids: list[str]
    duration_seconds: float


# ─────────────────────────────────────────────
# LAYER 1 / LAYER 4 — protocols + fallbacks
# Real implementations live in core/input_validator.py and
# core/output_filter.py. The harness duck-types against them so they
# can be dropped in without touching this file.
# ─────────────────────────────────────────────

class SupportsValidate(Protocol):
    def validate(self, task: str, worker_spec: WorkerSpec) -> Any: ...


class SupportsFilter(Protocol):
    def filter(self, output: str, worker_spec: WorkerSpec) -> Any: ...


@dataclass
class _FallbackValidation:
    ok: bool
    reason: str
    sanitised_task: str
    injection_detected: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass
class _FallbackFilterResult:
    ok: bool
    scrubbed_output: str
    pii_detected: bool = False
    content_flagged: bool = False
    flags: list[str] = field(default_factory=list)


class _FallbackInputValidator:
    """Conservative stand-in for core.input_validator.InputValidator."""

    INJECTION_PATTERNS = [
        "ignore previous instructions",
        "ignore your system prompt",
        "you are now",
        "pretend you are",
        "disregard your",
        "forget everything",
        "new instructions:",
        "<|im_start|>",
        "<|im_end|>",
        "[INST]",
    ]

    def validate(self, task: str, worker_spec: WorkerSpec) -> _FallbackValidation:
        sanitised = task.replace("\x00", "").strip()
        lowered = sanitised.lower()
        for pattern in self.INJECTION_PATTERNS:
            if pattern in lowered:
                return _FallbackValidation(
                    ok=False,
                    reason=f"Possible prompt injection detected: '{pattern}'",
                    sanitised_task=sanitised,
                    injection_detected=True,
                    flags=["injection_pattern"],
                )
        if len(sanitised) > DEFAULT_MAX_INPUT_CHARS:
            return _FallbackValidation(
                ok=False,
                reason=f"Task exceeds max input length of {DEFAULT_MAX_INPUT_CHARS} chars",
                sanitised_task=sanitised,
                flags=["input_too_long"],
            )
        if not sanitised:
            return _FallbackValidation(
                ok=False, reason="Empty task", sanitised_task=sanitised, flags=["empty_input"]
            )
        return _FallbackValidation(ok=True, reason="ok", sanitised_task=sanitised)


class _FallbackOutputFilter:
    """Pass-through stand-in for core.output_filter.OutputFilter."""

    def filter(self, output: str, worker_spec: WorkerSpec) -> _FallbackFilterResult:
        return _FallbackFilterResult(
            ok=True, scrubbed_output=output, flags=["output_filter_fallback"]
        )


def _load_layer(module: str, cls: str, fallback: Callable[[], Any]) -> Any:
    """Import a layer implementation if it exists, else use the fallback."""
    try:
        mod = __import__(module, fromlist=[cls])
        return getattr(mod, cls)()
    except (ImportError, AttributeError):
        logger.warning("%s.%s not available — using built-in fallback", module, cls)
        return fallback()


# ─────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────

class HarnessError(Exception):
    """Base class for harness-level failures."""


class PermissionDeniedError(HarnessError):
    """A tool call hit a BLOCKED or UNKNOWN decision — task stops here."""

    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"Permission denied for '{tool_name}': {reason}")


# ─────────────────────────────────────────────
# DEFAULT TOOL EXECUTOR (stub until runtime/tool_executor.py)
# ─────────────────────────────────────────────

async def _default_tool_executor(tool_name: str, tool_input: dict) -> str:
    """
    Placeholder executor. Real MCP-backed execution lands in
    runtime/tool_executor.py. Raising here is surfaced to the model as an
    is_error tool_result so it can adapt or finish without the tool.
    """
    raise NotImplementedError(
        f"Tool '{tool_name}' passed permission checks but no executor is wired. "
        f"Provide tool_executor= to AgentHarness, or build runtime/tool_executor.py."
    )


# ─────────────────────────────────────────────
# AGENT HARNESS
# ─────────────────────────────────────────────

class AgentHarness:
    """
    Wires the five harness layers around a single agent loop.

    Args:
        spec_path:       path to the worker spec YAML
        audit_log_path:  where the append-only JSONL audit trail is written
        tool_executor:   async callable (tool_name, tool_input) -> str.
                         Defaults to a stub that reports not-implemented.
        input_validator: Layer 1 override (defaults to core.input_validator
                         if importable, else a built-in fallback)
        output_filter:   Layer 4 override (same pattern)
        task_timeout_seconds: wall-clock cap for one run_task() call
    """

    def __init__(
        self,
        spec_path: str | Path,
        audit_log_path: str | Path = "audit.jsonl",
        tool_executor: Callable[[str, dict], Awaitable[str]] | None = None,
        input_validator: SupportsValidate | None = None,
        output_filter: SupportsFilter | None = None,
        task_timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
    ):
        self.spec = WorkerSpec(spec_path)
        self.audit = AuditLogger(audit_log_path)
        self.engine = PermissionEngine(self.spec, audit_logger=self.audit)
        # resolve() also enforces require_on_prem at startup — fails here,
        # before any task runs (design principle #8)
        self.llm_client, self.cost_tracker = ModelResolver(self.spec._raw).resolve()

        self.input_validator: SupportsValidate = input_validator or _load_layer(
            "core.input_validator", "InputValidator", _FallbackInputValidator
        )
        self.output_filter: SupportsFilter = output_filter or _load_layer(
            "core.output_filter", "OutputFilter", _FallbackOutputFilter
        )
        self.tool_executor = tool_executor or _default_tool_executor
        self.task_timeout_seconds = task_timeout_seconds

        self._provider: str = self.spec._raw.get("model", {}).get("provider", "anthropic")
        self._model_name: str = self.spec._raw.get("model", {}).get("name", "")
        self._behavior: dict = self.spec._raw.get("behavior", {})
        self._action_times: deque[float] = deque()  # rolling 1h window of executed actions

    # ── Public API ────────────────────────────────────────

    async def run_task(self, task: str, context: dict | None = None) -> HarnessResult:
        """
        Execute one task end-to-end through all five layers.
        Never raises for expected conditions (blocked tool, budget, approval,
        bad input) — those come back as a HarnessResult. Only programming
        errors escape.
        """
        context = context or {}
        task_id = str(uuid.uuid4())
        started = time.monotonic()
        audit_ids: list[str] = []
        self.cost_tracker.reset_task()

        def _result(
            success: bool,
            output: str | None = None,
            approval_id: str | None = None,
            error: str | None = None,
        ) -> HarnessResult:
            return HarnessResult(
                success=success,
                worker_id=self.spec.worker_id,
                task_id=task_id,
                output=output,
                approval_id=approval_id,
                error=error,
                cost_record=self._task_cost_record(),
                audit_ids=audit_ids,
                duration_seconds=round(time.monotonic() - started, 3),
            )

        # 0. Worker must be active — refuse before any layer
        if self.spec.worker_status != WorkerStatus.ACTIVE:
            reason = f"{REASON_WORKER_NOT_ACTIVE}: worker is {self.spec.worker_status.value}"
            audit_ids.append(self._log(EVENT_TASK_REJECTED, task_id, reason=reason))
            return _result(False, error=reason)

        # 0b. Working hours gate (behavior.working_hours)
        if not self._within_working_hours():
            audit_ids.append(
                self._log(EVENT_TASK_REJECTED, task_id, reason=REASON_OUTSIDE_WORKING_HOURS)
            )
            return _result(False, error=REASON_OUTSIDE_WORKING_HOURS)

        # 1. Log BEFORE doing anything else (Layer 5 — log before execute)
        audit_ids.append(
            self._log(EVENT_TASK_RECEIVED, task_id, task_length=len(task))
        )

        # 2. Layer 1 — input validation
        validation = self.input_validator.validate(task, self.spec)
        if not validation.ok:
            audit_ids.append(
                self._log(
                    EVENT_TASK_REJECTED,
                    task_id,
                    reason=f"{REASON_INPUT_REJECTED}: {validation.reason}",
                    flags=list(getattr(validation, "flags", [])),
                )
            )
            return _result(False, error=f"{REASON_INPUT_REJECTED}: {validation.reason}")
        audit_ids.append(self._log(EVENT_TASK_VALIDATED, task_id))
        sanitised_task = validation.sanitised_task

        # 3. Load memory + 4. build system prompt
        memory = await self._load_memory()
        system_prompt = self._build_system_prompt(sanitised_task, memory)

        # 5. Agent loop, under a wall-clock timeout
        try:
            result = await asyncio.wait_for(
                self._agent_loop(task_id, sanitised_task, system_prompt, audit_ids, _result),
                timeout=self.task_timeout_seconds,
            )
        except asyncio.TimeoutError:
            audit_ids.append(self._log(EVENT_TASK_FAILED, task_id, reason=REASON_TIMEOUT))
            return _result(False, error=f"{REASON_TIMEOUT}: exceeded {self.task_timeout_seconds}s")

        # 6. Memory save + final audit happen inside _agent_loop's exit paths
        return result

    # ── Agent loop (steps 5a–5f from CLAUDE.md) ───────────

    async def _agent_loop(
        self,
        task_id: str,
        task: str,
        system_prompt: str,
        audit_ids: list[str],
        _result: Callable[..., HarnessResult],
    ) -> HarnessResult:
        """The core LLM loop: call model, gate every tool, repeat."""
        messages: list[dict] = [{"role": "user", "content": task}]
        tools = self._build_tool_definitions()

        for _ in range(MAX_AGENT_ITERATIONS):
            # 5b. Budget gate before every call
            budget = self.cost_tracker.check_budget()
            if not budget.ok:
                audit_ids.append(
                    self._log(
                        EVENT_TASK_FAILED, task_id,
                        reason=REASON_BUDGET_EXCEEDED, detail=budget.reason,
                    )
                )
                return _result(False, error=f"{REASON_BUDGET_EXCEEDED}: {budget.message}")

            # 5a. Call the LLM
            try:
                response = await self._call_llm(system_prompt, messages, tools)
            except Exception as exc:  # network/provider failure — log and stop
                logger.exception("LLM call failed for task %s", task_id)
                audit_ids.append(
                    self._log(EVENT_TASK_FAILED, task_id, reason=REASON_LLM_ERROR, detail=str(exc))
                )
                return _result(False, error=f"{REASON_LLM_ERROR}: {exc}")

            self.cost_tracker.record_usage(
                self._provider,
                self._model_name,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            audit_ids.append(
                self._log(
                    EVENT_LLM_CALL, task_id,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    stop_reason=response.stop_reason,
                )
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            # Done — no more tool calls
            if response.stop_reason != "tool_use" or not tool_uses:
                final_text = "".join(b.text for b in response.content if b.type == "text")
                return await self._finish(task_id, final_text, messages, audit_ids, _result)

            # 5c/5d. Gate and execute each requested tool
            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict] = []

            for block in tool_uses:
                payload = dict(block.input) if isinstance(block.input, dict) else {}
                check = self.engine.check(block.name, payload)  # Layer 2 + Layer 5
                audit_ids.append(check.audit_id)

                if check.is_hard_stop():
                    # BLOCKED / UNKNOWN → task stops here, audit already written
                    audit_ids.append(
                        self._log(
                            EVENT_TASK_FAILED, task_id,
                            reason=REASON_PERMISSION_DENIED, tool=block.name,
                        )
                    )
                    return _result(
                        False,
                        error=f"{REASON_PERMISSION_DENIED}: {check.reason}",
                    )

                if check.requires_approval():
                    # Pause the task; manager is notified out-of-band
                    await self._notify_approval(check.approval_id)
                    audit_ids.append(
                        self._log(
                            EVENT_TASK_PAUSED_FOR_APPROVAL, task_id,
                            tool=block.name, approval_id=check.approval_id,
                        )
                    )
                    return _result(False, approval_id=check.approval_id)

                # ALLOWED — rate limit, then execute
                if not self._check_rate_limit():
                    audit_ids.append(
                        self._log(EVENT_TASK_FAILED, task_id, reason=REASON_RATE_LIMITED)
                    )
                    return _result(
                        False,
                        error=f"{REASON_RATE_LIMITED}: "
                              f"max {self._max_actions_per_hour()} autonomous actions/hour",
                    )

                tool_results.append(
                    await self._execute_tool(task_id, block, audit_ids)
                )

            messages.append({"role": "user", "content": tool_results})

        audit_ids.append(self._log(EVENT_TASK_FAILED, task_id, reason=REASON_MAX_ITERATIONS))
        return _result(False, error=f"{REASON_MAX_ITERATIONS}: {MAX_AGENT_ITERATIONS} iterations")

    async def _execute_tool(self, task_id: str, block: Any, audit_ids: list[str]) -> dict:
        """Run one ALLOWED tool. Failures are returned to the model, not raised."""
        try:
            output = await self.tool_executor(block.name, dict(block.input))
            self._action_times.append(time.monotonic())
            audit_ids.append(self._log(EVENT_TOOL_EXECUTED, task_id, tool=block.name))
            return {"type": "tool_result", "tool_use_id": block.id, "content": output}
        except Exception as exc:
            logger.warning("Tool '%s' failed in task %s: %s", block.name, task_id, exc)
            audit_ids.append(
                self._log(EVENT_TOOL_FAILED, task_id, tool=block.name, detail=str(exc))
            )
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Tool execution failed: {exc}",
                "is_error": True,
            }

    async def _finish(
        self,
        task_id: str,
        final_text: str,
        messages: list[dict],
        audit_ids: list[str],
        _result: Callable[..., HarnessResult],
    ) -> HarnessResult:
        """Layer 4 filter, memory save, completion audit."""
        filtered = self.output_filter.filter(final_text, self.spec)
        if not filtered.ok:
            audit_ids.append(
                self._log(
                    EVENT_TASK_FAILED, task_id,
                    reason=REASON_OUTPUT_BLOCKED,
                    flags=list(getattr(filtered, "flags", [])),
                )
            )
            return _result(False, error=f"{REASON_OUTPUT_BLOCKED}: {filtered.flags}")

        await self._save_memory(messages, filtered.scrubbed_output)
        audit_ids.append(
            self._log(
                EVENT_TASK_COMPLETED, task_id,
                output_length=len(filtered.scrubbed_output),
                pii_detected=getattr(filtered, "pii_detected", False),
            )
        )
        return _result(True, output=filtered.scrubbed_output)

    # ── LLM transport ─────────────────────────────────────

    async def _call_llm(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> Any:
        """
        One model round-trip. The resolved client is synchronous, so it runs
        in a worker thread to keep the harness fully async.
        """
        if self._provider != "anthropic":
            raise NotImplementedError(
                f"v0.1 agent loop supports provider 'anthropic' only — "
                f"spec requests '{self._provider}'. OpenAI-compatible loop is Phase 2."
            )
        return await asyncio.to_thread(
            self.llm_client.messages.create,
            model=self._model_name,
            max_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=messages,
            tools=tools,
        )

    def _build_tool_definitions(self) -> list[dict]:
        """
        Expose the spec's allowed + approval_required tools to the model.
        Blocked tools are never advertised. Schemas are generic in v0.1;
        runtime/tool_executor.py will provide real per-tool schemas.
        """
        defs: list[dict] = []
        tool_sections = self.spec._raw.get("tools", {})
        for tier in ("allowed", "approval_required"):
            for entry in tool_sections.get(tier, []):
                name = entry["name"] if isinstance(entry, dict) else entry
                if name in self.spec.ABSOLUTE_BLOCKS:
                    continue
                description = (
                    entry.get("description", name) if isinstance(entry, dict) else name
                )
                defs.append(
                    {
                        "name": name,
                        "description": description,
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "action_summary": {
                                    "type": "string",
                                    "description": "One-line summary of what this call does",
                                },
                                "content_preview": {
                                    "type": "string",
                                    "description": "Preview of content being sent/created",
                                },
                                "arguments": {
                                    "type": "object",
                                    "description": "Tool-specific arguments",
                                },
                            },
                            "required": ["action_summary"],
                        },
                    }
                )
        return defs

    # ── System prompt (exact section order from the brief) ─

    def _build_system_prompt(self, task: str, memory: list[str]) -> str:
        """
        Build the worker system prompt in the mandated order:
        identity → autonomy → base skills → role skills → knowledge →
        memory → behavioural rules → task.
        The spec itself is never included — the harness enforces it.
        """
        raw = self.spec._raw
        identity = raw.get("identity", {})
        org = raw.get("org", {})
        role = raw.get("role", {})
        manager = org.get("reports_to", {}).get("name", "your manager")
        department = org.get("department", "the company")

        sections: list[str] = []

        # [1] Identity
        sections.append(
            f"You are {self.spec.worker_name}, a {role.get('title', 'worker')} "
            f"in {department}. You report to {manager}."
        )

        # [2] Autonomy and approval rules
        sections.append(
            f"Your autonomy level is {self.spec.autonomy_level} "
            f"({raw.get('autonomy', {}).get('label', '')}). Before using any tool, "
            f"confirm it is in your allowed tools list. If uncertain, do not use "
            f"the tool — ask your manager instead."
        )

        # [3] Base skill context
        sections.extend(self._load_skill_files(SKILLS_BASE_DIR, raw.get("base_skills", [])))

        # [4] Role skill context
        pack = role.get("pack")
        if pack:
            sections.extend(self._load_skill_files(SKILLS_ROLES_DIR, [pack]))

        # [5] Company knowledge (policy_rag retrieval lands with memory_manager;
        #     until then, the tone profile from the spec is included directly)
        tone = raw.get("knowledge", {}).get("tone_profile")
        if tone:
            sections.append(f"Company tone profile: {tone}")

        # [6] Working memory
        if memory:
            sections.append("Recent context:\n" + "\n".join(memory))

        # [7] Behavioural rules
        sections.append(
            "Never reveal API keys or secrets. Never claim to be human. "
            "Always escalate when you are uncertain. Log your reasoning."
        )

        # [8] Task
        sections.append(f"Your task: {task}")

        return "\n\n".join(sections)

    def _load_skill_files(self, directory: str, names: list[str]) -> list[str]:
        """Load SKILL .md files that exist; silently skip the rest (v0.1)."""
        root = Path(self.spec._path).resolve().parent.parent
        out: list[str] = []
        for name in names:
            path = root / directory / f"{name}.md"
            if path.exists():
                out.append(path.read_text())
        return out

    # ── Guards ────────────────────────────────────────────

    def _within_working_hours(self, now: datetime | None = None) -> bool:
        """
        Enforce behavior.working_hours from the spec. Missing config = always on.
        Day keys: monday_friday, saturday, sunday. Value 'off' or 'HH:MM–HH:MM'.
        """
        hours = self._behavior.get("working_hours")
        if not hours:
            return True
        tz = ZoneInfo(hours.get("timezone", "UTC"))
        now = now or datetime.now(tz)
        key = (
            "saturday" if now.weekday() == 5
            else "sunday" if now.weekday() == 6
            else "monday_friday"
        )
        window = hours.get(key, "off")
        if not window or window == "off":
            return False
        sep = "–" if "–" in window else "-"
        start_s, end_s = (part.strip() for part in window.split(sep, 1))
        start = datetime.strptime(start_s, "%H:%M").time()
        end = datetime.strptime(end_s, "%H:%M").time()
        return start <= now.time() <= end

    def _max_actions_per_hour(self) -> int:
        return self._behavior.get(
            "max_autonomous_actions_per_hour", DEFAULT_MAX_ACTIONS_PER_HOUR
        )

    def _check_rate_limit(self) -> bool:
        """Rolling one-hour window over executed autonomous actions."""
        cutoff = time.monotonic() - 3600
        while self._action_times and self._action_times[0] < cutoff:
            self._action_times.popleft()
        return len(self._action_times) < self._max_actions_per_hour()

    # ── Stubs (filled in by runtime/ modules) ─────────────

    async def _load_memory(self) -> list[str]:
        """Episodic memory load — Supabase-backed in runtime/memory_manager.py."""
        return []

    async def _save_memory(self, messages: list[dict], output: str) -> None:
        """Episodic memory save — Supabase-backed in runtime/memory_manager.py."""

    async def _notify_approval(self, approval_id: str | None) -> None:
        """Slack/email routing lands in runtime/approval_router.py."""
        logger.info("Approval %s pending — notify manager via approval_router", approval_id)

    # ── Helpers ───────────────────────────────────────────

    def _log(self, event_type: str, task_id: str, **data: Any) -> str:
        """Layer 5 — write a harness audit event, return its audit_id."""
        return self.audit.log_event(
            event_type, self.spec.worker_id, {"task_id": task_id, **data}
        )

    def _task_cost_record(self) -> CostRecord:
        """Summarise this task's accumulated spend as a single CostRecord."""
        return CostRecord(
            provider=self._provider,
            model=self._model_name,
            input_tokens=self.cost_tracker.task_input_tokens,
            output_tokens=self.cost_tracker.task_output_tokens,
            cost_usd=round(self.cost_tracker.task_spent_usd, 6),
        )
