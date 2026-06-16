"""
OpenWorker Tool Access Request — core/tool_access_request.py
=================================================================
A worker can ask for tools it doesn't currently have, when it believes
they'd meaningfully improve its output (docs/CLAUDE_EXTENSION.md
Decision 5). This NEVER auto-grants anything — it creates a pending
request a manager reviews via Slack (or stdout, same fallback pattern
as ApprovalRequest). The worker spec itself is only ever edited by a
human, outside of this flow.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from core.audit_logger import AuditLogger

logger = logging.getLogger("openworker.tool_access_request")


@dataclass
class ToolAccessRequest:
    """Created when a worker calls the always-allowed `request_tool_access` tool."""

    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    worker_id: str = ""
    worker_name: str = ""
    tools_requested: list[str] = field(default_factory=list)
    justification: str = ""
    task_context: str = ""
    suggested_tier: str = "approval_required"
    status: str = "pending"
    approved_tools: list[str] = field(default_factory=list)
    rejected_tools: list[str] = field(default_factory=list)
    manager_note: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: str | None = None

    def to_slack_message(self) -> dict:
        """Formats as a Slack Block Kit message for manager review."""
        return {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{self.worker_name} is requesting new tool access",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Tools requested:*\n" + "\n".join(f"• `{t}`" for t in self.tools_requested),
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Why:*\n{self.justification}"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Context:*\n{self.task_context}"},
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve All"},
                            "style": "primary",
                            "value": f"approve_tools:{self.request_id}",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Review Each"},
                            "value": f"review_tools:{self.request_id}",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Reject All"},
                            "style": "danger",
                            "value": f"reject_tools:{self.request_id}",
                        },
                    ],
                },
            ]
        }

    def to_row(self) -> dict:
        """Shape matching the `tool_access_requests` table (database/schema.sql)."""
        return {
            "request_id": self.request_id,
            "worker_id": self.worker_id,
            "worker_name": self.worker_name,
            "tools_requested": self.tools_requested,
            "justification": self.justification,
            "task_context": self.task_context,
            "suggested_tier": self.suggested_tier,
            "status": self.status,
            "approved_tools": self.approved_tools,
            "rejected_tools": self.rejected_tools,
            "manager_note": self.manager_note,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


def apply_approved_tools(
    spec_path: str | Path,
    request: ToolAccessRequest,
    approved_tools: list[str],
    tier: str = "approval_required",
) -> dict:
    """
    Edits the worker spec YAML to add `approved_tools` to the given tier
    and writes it back to disk. Called ONLY from a human-approval flow
    (e.g. the Slack "Approve" button handler) — never by the worker
    itself (design principle 5: a worker's spec is read-only to it).

    Tools already present in allowed/approval_required/blocked are left
    untouched. Returns the updated raw spec dict for the caller to audit-log.
    """
    path = Path(spec_path)
    with open(path) as f:
        raw = yaml.safe_load(f)

    existing_names = set()
    for existing_tier in ("allowed", "approval_required", "blocked"):
        for entry in raw.get("tools", {}).get(existing_tier, []):
            existing_names.add(entry["name"] if isinstance(entry, dict) else entry)

    raw.setdefault("tools", {}).setdefault(tier, [])
    for tool_name in approved_tools:
        if tool_name in existing_names:
            logger.info("Tool '%s' already present in spec for %s — skipping", tool_name, request.worker_name)
            continue
        raw["tools"][tier].append({
            "name": tool_name,
            "description": f"Granted via tool access request {request.request_id}",
        })

    with open(path, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)

    return raw


def resolve_tool_access_request(
    request: ToolAccessRequest,
    spec_path: str | Path,
    approved_tools: list[str],
    rejected_tools: list[str],
    resolved_by: str,
    audit_logger: "AuditLogger | None" = None,
    manager_note: str | None = None,
) -> ToolAccessRequest:
    """
    Resolves a pending ToolAccessRequest: updates the spec for approved
    tools, marks the rest rejected, and writes an audit entry. Never
    auto-grants anything by itself — this is the function a manager's
    "Approve" / "Reject" action in Slack (or any future API) calls.
    """
    request.approved_tools = approved_tools
    request.rejected_tools = rejected_tools
    request.manager_note = manager_note
    request.resolved_at = datetime.now(timezone.utc).isoformat()
    request.status = (
        "approved" if approved_tools and not rejected_tools else
        "rejected" if rejected_tools and not approved_tools else
        "partially_approved"
    )

    if approved_tools:
        apply_approved_tools(spec_path, request, approved_tools, tier=request.suggested_tier)

    if audit_logger is not None:
        audit_logger.log_event(
            "tool_access_resolved",
            request.worker_id,
            {
                "request_id": request.request_id,
                "approved_tools": approved_tools,
                "rejected_tools": rejected_tools,
                "resolved_by": resolved_by,
            },
        )

    return request
