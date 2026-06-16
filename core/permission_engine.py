"""
OpenWorker Permission Engine v0.1
==================================
The single source of truth for what a worker can and cannot do.

Every tool call made by any worker MUST pass through this engine.
No exceptions. No bypasses. No prompt can override a BLOCKED decision.

Architecture:
  WorkerSpec       — loads and validates the YAML worker file
  PermissionEngine — evaluates every tool call request
  ApprovalRequest  — represents a pending human approval
  AuditLogger      — writes every decision to the audit trail
  EngineResult     — the typed response returned for every check

Decision flow:
  BLOCKED   → hard stop, no human can override, log and raise
  ALLOWED   → execute immediately, log action
  APPROVAL  → pause, create ApprovalRequest, wait for human signal
  UNKNOWN   → treat as BLOCKED (fail-safe default)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml  # pip install pyyaml

# AuditLogger lives in core/audit_logger.py (Layer 5). Re-exported here
# so existing `from core.permission_engine import AuditLogger` keeps working.
from core.audit_logger import AuditLogger


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class Decision(str, Enum):
    ALLOWED   = "allowed"
    APPROVAL  = "approval_required"
    BLOCKED   = "blocked"
    UNKNOWN   = "unknown"     # tool not in spec — defaults to BLOCKED


class ApprovalStatus(str, Enum):
    PENDING   = "pending"
    APPROVED  = "approved"
    REJECTED  = "rejected"
    MODIFIED  = "modified"   # approved but manager changed the content
    EXPIRED   = "expired"    # SLA passed without response


class WorkerStatus(str, Enum):
    ACTIVE      = "active"
    SUSPENDED   = "suspended"
    ONBOARDING  = "onboarding"
    RETIRED     = "retired"


# ─────────────────────────────────────────────
# RESULT TYPES
# ─────────────────────────────────────────────

@dataclass
class EngineResult:
    """
    Returned by PermissionEngine.check() for every tool call.
    The runtime reads this and decides what to do next.
    """
    decision:        Decision
    tool_name:       str
    worker_id:       str
    reason:          str
    audit_id:        str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:       str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approval_id:     str | None = None   # set if decision == APPROVAL
    sla_hours:       int | None = None
    approver_email:  str | None = None
    payload_hash:    str | None = None   # SHA-256 of the action payload

    def is_executable(self) -> bool:
        return self.decision == Decision.ALLOWED

    def requires_approval(self) -> bool:
        return self.decision == Decision.APPROVAL

    def is_hard_stop(self) -> bool:
        return self.decision in (Decision.BLOCKED, Decision.UNKNOWN)

    def to_dict(self) -> dict:
        return {
            "audit_id":       self.audit_id,
            "timestamp":      self.timestamp,
            "decision":       self.decision.value,
            "tool_name":      self.tool_name,
            "worker_id":      self.worker_id,
            "reason":         self.reason,
            "approval_id":    self.approval_id,
            "sla_hours":      self.sla_hours,
            "approver_email": self.approver_email,
            "payload_hash":   self.payload_hash,
        }


@dataclass
class ApprovalRequest:
    """
    Created when a tool requires human sign-off.
    Stored in the database, sent to the manager via Slack/email.
    """
    approval_id:     str = field(default_factory=lambda: str(uuid.uuid4()))
    worker_id:       str = ""
    worker_name:     str = ""
    tool_name:       str = ""
    action_summary:  str = ""
    content_preview: str = ""
    approver_email:  str = ""
    backup_email:    str | None = None
    sla_hours:       int = 4
    status:          ApprovalStatus = ApprovalStatus.PENDING
    created_at:      str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at:     str | None = None
    resolved_by:     str | None = None
    manager_note:    str | None = None

    def is_expired(self) -> bool:
        from datetime import timedelta
        created = datetime.fromisoformat(self.created_at)
        deadline = created + timedelta(hours=self.sla_hours)
        return datetime.now(timezone.utc) > deadline and self.status == ApprovalStatus.PENDING

    def to_slack_message(self) -> dict:
        """Formats the approval request as a Slack Block Kit message."""
        return {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"{self.worker_name} is requesting approval"}
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Tool:*\n`{self.tool_name}`"},
                        {"type": "mrkdwn", "text": f"*SLA:*\n{self.sla_hours} hours"},
                    ]
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Action:*\n{self.action_summary}"}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Preview:*\n```{self.content_preview[:500]}```"}
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "value": f"approve:{self.approval_id}"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "style": "danger",
                            "value": f"reject:{self.approval_id}"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Request Changes"},
                            "value": f"modify:{self.approval_id}"
                        },
                    ]
                }
            ]
        }


# ─────────────────────────────────────────────
# WORKER SPEC LOADER
# ─────────────────────────────────────────────

class WorkerSpec:
    """
    Loads, validates, and provides typed access to a worker YAML file.
    This is the single source of truth for a worker's permissions.
    """

    # Tools that are ALWAYS blocked regardless of what the spec says.
    # Even if someone manually adds these to tools.allowed in the YAML,
    # the engine will override it with BLOCKED.
    ABSOLUTE_BLOCKS = frozenset({
        "payroll_systems",
        "hr_systems",
        "production_databases",
        "financial_erp",
        "customer_pii_export",
        "infrastructure_tools",
        "legal_document_signing",
    })

    def __init__(self, yaml_path: str | Path):
        self._path = Path(yaml_path)
        self._raw = self._load()
        self._validate()
        self._allowed:  set[str] = self._parse_allowed()
        self._approval: dict[str, dict] = self._parse_approval()
        self._blocked:  set[str] = self._parse_blocked()

    def _load(self) -> dict:
        with open(self._path) as f:
            data = yaml.safe_load(f)
        if not data:
            raise ValueError(f"Empty or invalid YAML at {self._path}")
        return data

    def _validate(self):
        required = ["apiVersion", "kind", "identity", "org", "role",
                    "autonomy", "tools", "approvals", "audit", "runtime"]
        for field in required:
            if field not in self._raw:
                raise ValueError(f"Worker spec missing required field: '{field}'")
        if self._raw.get("apiVersion") != "openworker/v1":
            raise ValueError("apiVersion must be 'openworker/v1'")
        if self._raw.get("kind") != "Worker":
            raise ValueError("kind must be 'Worker'")

    def _parse_allowed(self) -> set[str]:
        tools = self._raw.get("tools", {}).get("allowed", [])
        names = {t["name"] if isinstance(t, dict) else t for t in tools}
        # Enforce absolute blocks — remove anything that shouldn't be here
        return names - self.ABSOLUTE_BLOCKS

    def _parse_approval(self) -> dict[str, dict]:
        tools = self._raw.get("tools", {}).get("approval_required", [])
        result = {}
        for t in tools:
            name = t["name"] if isinstance(t, dict) else t
            if name not in self.ABSOLUTE_BLOCKS:
                result[name] = t if isinstance(t, dict) else {"name": name}
        return result

    def _parse_blocked(self) -> set[str]:
        spec_blocked = self._raw.get("tools", {}).get("blocked", [])
        names = {t if isinstance(t, str) else t.get("name", "") for t in spec_blocked}
        return names | self.ABSOLUTE_BLOCKS   # always union with hard blocks

    # ── Public accessors ──────────────────────────────────

    @property
    def worker_id(self) -> str:
        return self._raw["identity"].get("employee_id", self.worker_name)

    @property
    def worker_name(self) -> str:
        return self._raw["identity"]["name"]

    @property
    def worker_status(self) -> WorkerStatus:
        status = self._raw["identity"].get("status", "active")
        return WorkerStatus(status)

    @property
    def autonomy_level(self) -> str:
        return self._raw["autonomy"]["level"]

    @property
    def approver_email(self) -> str:
        return self._raw["org"]["reports_to"]["email"]

    @property
    def backup_approver_email(self) -> str | None:
        backup = self._raw["org"].get("backup_approver")
        return backup["email"] if backup else None

    @property
    def approval_channel(self) -> str:
        return self._raw["approvals"].get("channel", "slack")

    def get_approval_config(self, tool_name: str) -> dict:
        return self._approval.get(tool_name, {})

    def classify(self, tool_name: str) -> Decision:
        """
        Classify a tool name into a decision tier.
        Order matters: BLOCKED first, then ALLOWED, then APPROVAL, else UNKNOWN.
        """
        if tool_name in self._blocked:
            return Decision.BLOCKED
        if tool_name in self._allowed:
            return Decision.ALLOWED
        if tool_name in self._approval:
            return Decision.APPROVAL
        return Decision.UNKNOWN


# ─────────────────────────────────────────────
# PERMISSION ENGINE
# ─────────────────────────────────────────────

class PermissionEngine:
    """
    The core enforcement layer.
    Every tool call from every worker passes through engine.check().

    Usage:
        spec   = WorkerSpec("worker.maya.yaml")
        engine = PermissionEngine(spec)

        result = engine.check("linkedin_publish", payload={"content": "..."})

        if result.is_executable():
            execute_tool(...)
        elif result.requires_approval():
            send_approval_request(result.approval_id)
            wait_for_signal()
        else:
            raise PermissionDeniedError(result.reason)
    """

    def __init__(self, spec: WorkerSpec, audit_logger: AuditLogger | None = None):
        self._spec   = spec
        self._logger = audit_logger or AuditLogger()
        self._pending_approvals: dict[str, ApprovalRequest] = {}

    # ── Core decision method ──────────────────────────────

    def check(self, tool_name: str, payload: dict | None = None) -> EngineResult:
        """
        The single entry point for all permission checks.
        Call this before executing ANY tool.
        Returns an EngineResult — never raises on its own.
        The caller decides what to do with the result.
        """
        # 0. Worker must be active
        if self._spec.worker_status != WorkerStatus.ACTIVE:
            result = EngineResult(
                decision=Decision.BLOCKED,
                tool_name=tool_name,
                worker_id=self._spec.worker_id,
                reason=f"Worker is {self._spec.worker_status.value} — all actions suspended",
            )
            self._logger.log(result, payload)
            return result

        # 1. Classify the tool
        decision = self._spec.classify(tool_name)

        # 2. Build result based on classification
        if decision == Decision.BLOCKED:
            result = EngineResult(
                decision=Decision.BLOCKED,
                tool_name=tool_name,
                worker_id=self._spec.worker_id,
                reason=f"'{tool_name}' is in the blocked tier. "
                       f"No instruction can override this. "
                       f"Edit the worker spec to change permissions.",
            )

        elif decision == Decision.UNKNOWN:
            result = EngineResult(
                decision=Decision.UNKNOWN,
                tool_name=tool_name,
                worker_id=self._spec.worker_id,
                reason=f"'{tool_name}' is not defined in the worker spec. "
                       f"Unknown tools default to BLOCKED (fail-safe). "
                       f"Add it to tools.allowed or tools.approval_required to enable.",
            )

        elif decision == Decision.ALLOWED:
            result = EngineResult(
                decision=Decision.ALLOWED,
                tool_name=tool_name,
                worker_id=self._spec.worker_id,
                reason=f"'{tool_name}' is in the allowed tier. Executing.",
            )

        elif decision == Decision.APPROVAL:
            config = self._spec.get_approval_config(tool_name)
            approval = self._create_approval_request(tool_name, config, payload)
            self._pending_approvals[approval.approval_id] = approval
            result = EngineResult(
                decision=Decision.APPROVAL,
                tool_name=tool_name,
                worker_id=self._spec.worker_id,
                reason=f"'{tool_name}' requires manager approval before execution.",
                approval_id=approval.approval_id,
                sla_hours=config.get("sla_hours", 4),
                approver_email=self._spec.approver_email,
            )

        else:
            # Should never reach here — safety net
            result = EngineResult(
                decision=Decision.BLOCKED,
                tool_name=tool_name,
                worker_id=self._spec.worker_id,
                reason="Unhandled classification — defaulting to BLOCKED.",
            )

        self._logger.log(result, payload)
        return result

    # ── Approval lifecycle ────────────────────────────────

    def resolve_approval(
        self,
        approval_id: str,
        status: ApprovalStatus,
        resolved_by: str,
        manager_note: str | None = None,
    ) -> ApprovalRequest:
        """
        Called when a manager approves, rejects, or requests changes.
        Triggers trust score update.
        """
        approval = self._pending_approvals.get(approval_id)
        if not approval:
            raise ValueError(f"Approval ID '{approval_id}' not found or already resolved.")

        approval.status      = status
        approval.resolved_at = datetime.now(timezone.utc).isoformat()
        approval.resolved_by = resolved_by
        approval.manager_note = manager_note

        self._logger.log_approval_event(approval, event=status.value, by=resolved_by)
        self._update_trust_score(status)

        return approval

    def get_pending_approvals(self) -> list[ApprovalRequest]:
        """Returns all pending approvals — used by the Manager Dashboard."""
        return [a for a in self._pending_approvals.values()
                if a.status == ApprovalStatus.PENDING]

    def expire_stale_approvals(self):
        """
        Called by a scheduler (e.g. every 30 min via n8n).
        Marks expired approvals and escalates to backup approver.
        Never auto-approves. Always notifies.
        """
        for approval in self._pending_approvals.values():
            if approval.is_expired():
                approval.status = ApprovalStatus.EXPIRED
                self._logger.log_approval_event(
                    approval, event="expired_escalated_to_backup",
                    by="system"
                )
                # Caller handles actual notification to backup_approver

    # ── Trust score (lightweight, local) ─────────────────

    def _update_trust_score(self, status: ApprovalStatus):
        """
        Nudges the trust score up or down based on each approval outcome.
        Full recalculation happens monthly via the performance review job.
        """
        current = self._spec._raw["autonomy"].get("trust_score", 75)

        if status == ApprovalStatus.APPROVED:
            new_score = min(100, current + 0.5)
        elif status == ApprovalStatus.REJECTED:
            new_score = max(0, current - 2.0)
        elif status == ApprovalStatus.MODIFIED:
            new_score = max(0, current - 0.5)
        else:
            new_score = current

        self._spec._raw["autonomy"]["trust_score"] = round(new_score, 1)

    # ── Helpers ───────────────────────────────────────────

    def _create_approval_request(
        self,
        tool_name: str,
        config: dict,
        payload: dict | None,
    ) -> ApprovalRequest:
        summary = payload.get("action_summary", f"Execute {tool_name}") if payload else f"Execute {tool_name}"
        preview = payload.get("content_preview", "") if payload else ""

        return ApprovalRequest(
            worker_id       = self._spec.worker_id,
            worker_name     = self._spec.worker_name,
            tool_name       = tool_name,
            action_summary  = summary,
            content_preview = preview,
            approver_email  = self._spec.approver_email,
            backup_email    = self._spec.backup_approver_email,
            sla_hours       = config.get("sla_hours", 4),
        )


# ─────────────────────────────────────────────
# EXCEPTION
# ─────────────────────────────────────────────

class PermissionDeniedError(Exception):
    """
    Raised by the runtime when it encounters a BLOCKED or UNKNOWN result
    and there is no recovery path. The worker's task stops here.
    The audit log already has the record before this is raised.
    """
    def __init__(self, result: EngineResult):
        self.result = result
        super().__init__(
            f"[OpenWorker] Permission denied: {result.tool_name} → {result.decision.value}. "
            f"Reason: {result.reason}"
        )


# ─────────────────────────────────────────────
# QUICK DEMO
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    spec_path = sys.argv[1] if len(sys.argv) > 1 else "worker.maya.yaml"

    print("=" * 60)
    print("OpenWorker Permission Engine — Demo")
    print("=" * 60)

    try:
        spec   = WorkerSpec(spec_path)
        engine = PermissionEngine(spec)

        test_tools = [
            ("web_search",          {"action_summary": "Search competitor campaigns"}),
            ("linkedin_draft",      {"action_summary": "Draft Q3 product launch post"}),
            ("linkedin_publish",    {"action_summary": "Publish to company LinkedIn page",
                                     "content_preview": "Excited to announce our Q3 launch..."}),
            ("email_send_external", {"action_summary": "Send campaign to 4,200 subscribers",
                                     "content_preview": "Subject: Your August newsletter is here"}),
            ("payroll_systems",     {"action_summary": "Access payroll data"}),
            ("unknown_tool_xyz",    {"action_summary": "Some unrecognized tool"}),
        ]

        for tool_name, payload in test_tools:
            result = engine.check(tool_name, payload)
            icon = {"allowed": "✅", "approval_required": "⏳", "blocked": "🚫", "unknown": "❌"}.get(result.decision.value, "?")
            print(f"\n{icon}  {tool_name}")
            print(f"   Decision : {result.decision.value.upper()}")
            print(f"   Reason   : {result.reason}")
            if result.approval_id:
                print(f"   Approval : {result.approval_id} → notify {result.approver_email}")

    except FileNotFoundError:
        print(f"Worker spec not found: {spec_path}")
        print("Run from the same directory as worker.maya.yaml")
