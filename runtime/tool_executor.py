"""
OpenWorker Tool Executor — runtime/tool_executor.py
====================================================
Maps tool_name strings to real implementations. Called by AgentHarness
after PermissionEngine has already returned ALLOWED for a tool call —
permission enforcement is not this module's job.

As of the connectors/ extension, the actual provider logic (Brave vs
DuckDuckGo, Slack bot vs webhook vs stdout, ...) lives in
connectors/providers/ behind a ConnectorRegistry. This module just
adapts worker-spec tool-call payloads into the shape each capability's
connector expects, and adapts ConnectorResult back into ToolResult.

Every path degrades gracefully when its provider isn't configured —
the demo must work end-to-end with only ANTHROPIC_API_KEY set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from connectors.registry import ConnectorRegistry
from core.permission_engine import WorkerSpec

logger = logging.getLogger("openworker.tool_executor")


@dataclass
class ToolResult:
    """Returned by every handler in ToolExecutor."""
    success: bool
    output: str = ""
    error: str = ""


class ToolExecutor:
    """
    Dispatches an ALLOWED tool call to its connector.

    Usage:
        executor = ToolExecutor(task_id="abc123")
        result = await executor.execute("web_search", {"arguments": {"query": "..."}}, spec)
    """

    def __init__(self, task_id: str = "adhoc", registry: ConnectorRegistry | None = None):
        self.task_id = task_id
        self.registry = registry or ConnectorRegistry()

    async def execute(self, tool_name: str, payload: dict, worker_spec: WorkerSpec) -> ToolResult:
        """
        Run one tool call. Never raises — failures come back as
        ToolResult(success=False, error=...).
        """
        try:
            connector_payload = self._build_payload(tool_name, payload, worker_spec)
            connector = self.registry.get_connector(tool_name)
            if not connector.is_configured():
                logger.info("'%s' has no configured connector — %s", tool_name, connector.fallback_message())
                return ToolResult(success=False, error=connector.fallback_message())

            result = await connector.execute(connector_payload)
            return ToolResult(success=result.success, output=result.output, error=result.error or "")
        except Exception as exc:  # connector bug — still never raise out of execute()
            logger.exception("Tool '%s' raised unexpectedly", tool_name)
            return ToolResult(success=False, error=f"Unhandled error in '{tool_name}': {exc}")

    def _build_payload(self, tool_name: str, payload: dict, worker_spec: WorkerSpec) -> dict:
        """Flatten worker-spec tool args + add task_id/recipient defaults the connectors expect."""
        args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        flat = {**payload, **args, "task_id": self.task_id}

        if tool_name in ("slack_post_dm", "slack_post_channel"):
            flat.setdefault("action", "post_dm")
            flat.setdefault("recipient", worker_spec.approver_email)
        elif tool_name == "slack_read":
            flat.setdefault("action", "read")
        elif tool_name == "email_draft":
            flat.setdefault("kind", "email_draft")

        return flat


def make_harness_executor(
    worker_spec: WorkerSpec, task_id: str = "adhoc"
) -> Callable[[str, dict], Awaitable[str]]:
    """
    Adapts ToolExecutor.execute(name, payload, spec) -> ToolResult to the
    (name, payload) -> str shape AgentHarness expects for tool_executor=.
    A failed ToolResult is raised as an exception so the harness's existing
    is_error tool_result handling in _execute_tool applies unchanged.
    """
    executor = ToolExecutor(task_id=task_id)

    async def _run(tool_name: str, tool_input: dict) -> str:
        result = await executor.execute(tool_name, tool_input, worker_spec)
        if not result.success:
            raise RuntimeError(result.error)
        return result.output

    return _run
