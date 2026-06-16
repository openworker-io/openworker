"""
OpenWorker Approval Router — runtime/approval_router.py
=========================================================
Delivers an ApprovalRequest (or a ToolAccessRequest) to a human.
Supports Slack (bot token or incoming webhook) with a stdout fallback
that always works, even with no Slack configuration at all — the
demo must never fail because Slack isn't set up.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import aiohttp

from core.permission_engine import ApprovalRequest, WorkerSpec
from core.tool_access_request import ToolAccessRequest

logger = logging.getLogger("openworker.approval_router")


class _HasSlackMessage(Protocol):
    def to_slack_message(self) -> dict: ...


class ApprovalRouter:
    """
    Routes ApprovalRequest / ToolAccessRequest objects to the configured
    approval channel. Currently supports: slack, stdout (fallback).

    Usage:
        router = ApprovalRouter(spec)
        delivered = await router.send(approval_request)
        delivered = await router.send_tool_access_request(tool_access_request)
    """

    def __init__(self, spec: WorkerSpec):
        self.channel = spec._raw.get("approvals", {}).get("channel", "stdout")
        self.slack_channel = spec._raw.get("approvals", {}).get("slack_channel", "#approvals")
        self.slack_token = os.environ.get("SLACK_BOT_TOKEN")
        self.slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")

    async def send(self, approval: ApprovalRequest) -> bool:
        """Deliver an approval request. Always returns True — never blocks the task on delivery failure."""
        delivered = await self._deliver(approval, on_slack_failure=f"approval {approval.approval_id}")
        if not delivered:
            self._send_approval_stdout(approval)
        return True

    async def send_tool_access_request(self, request: ToolAccessRequest) -> bool:
        """Deliver a tool access request. Never auto-grants anything — purely a notification."""
        delivered = await self._deliver(request, on_slack_failure=f"tool access request {request.request_id}")
        if not delivered:
            self._send_tool_access_stdout(request)
        return True

    async def _deliver(self, request: _HasSlackMessage, on_slack_failure: str) -> bool:
        if self.channel != "slack" or not (self.slack_token or self.slack_webhook):
            return False
        delivered = await self._send_slack(request)
        if not delivered:
            logger.warning("Slack delivery failed for %s — falling back to stdout", on_slack_failure)
        return delivered

    async def _send_slack(self, request: _HasSlackMessage) -> bool:
        message = request.to_slack_message()
        try:
            async with aiohttp.ClientSession() as session:
                if self.slack_webhook:
                    async with session.post(self.slack_webhook, json=message) as resp:
                        return resp.status == 200
                async with session.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {self.slack_token}"},
                    json={"channel": self.slack_channel, **message},
                ) as resp:
                    data = await resp.json()
                    return bool(data.get("ok", False))
        except aiohttp.ClientError as exc:
            logger.warning("Slack API request errored: %s", exc)
            return False

    def _send_approval_stdout(self, approval: ApprovalRequest) -> None:
        """Clean terminal output so the demo works without Slack configured."""
        lines = [
            "",
            "=" * 60,
            "APPROVAL REQUIRED",
            f"Worker : {approval.worker_name}",
            f"Action : {approval.tool_name}",
            f"Summary: {approval.action_summary}",
            f"Preview: {approval.content_preview[:200]}",
            f"Approve: openworker approve {approval.approval_id}",
            f"Reject : openworker reject {approval.approval_id}",
            "=" * 60,
            "",
        ]
        for line in lines:
            logger.info(line)

    def _send_tool_access_stdout(self, request: ToolAccessRequest) -> None:
        lines = [
            "",
            "=" * 60,
            "TOOL ACCESS REQUESTED",
            f"Worker  : {request.worker_name}",
            f"Tools   : {', '.join(request.tools_requested)}",
            f"Why     : {request.justification}",
            f"Context : {request.task_context[:200]}",
            f"Review  : openworker tool-access review {request.request_id}",
            "=" * 60,
            "",
        ]
        for line in lines:
            logger.info(line)
