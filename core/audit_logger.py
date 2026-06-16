"""
OpenWorker Audit Logger v0.1 — Harness Layer 5
===============================================
Append-only JSONL audit trail. Every permission decision, harness event,
and approval lifecycle change is written here BEFORE the action executes.

Design principles enforced by this module:
- Log before execute — callers write the entry first, then act.
- Append-only — entries are never updated or deleted by the runtime.
- No raw content — payloads are stored as SHA-256 hashes only.

In production this should write to Supabase or a SIEM; the JSONL file
is the v0.1 reference implementation and the local dev path.

Extracted from core/permission_engine.py (first refactor step per the
project brief). permission_engine re-exports AuditLogger so existing
imports keep working.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # type-only — avoids a circular import with permission_engine
    from core.permission_engine import ApprovalRequest, EngineResult


class AuditLogger:
    """
    Writes every permission decision to an append-only JSONL audit log.
    task_logger skill in the spec means this cannot be bypassed.
    """

    def __init__(self, log_path: str | Path = "audit.jsonl"):
        self._path = Path(log_path)

    def log(self, result: "EngineResult", payload: dict | None = None) -> None:
        """Record a permission decision (Layer 2 output)."""
        entry = result.to_dict()
        if payload:
            # Never log raw payload — only a hash for verification
            entry["payload_hash"] = hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest()
        entry["payload_logged"] = False  # PII scrubbing: never store content
        self._append(entry)

    def log_event(self, event_type: str, worker_id: str, data: dict | None = None) -> str:
        """
        Generic harness-level audit event (task received, LLM call, etc.).
        Returns the audit_id so callers can build a full trail.
        Content values are never logged raw — callers pass metadata only.
        """
        audit_id = str(uuid.uuid4())
        self._append({
            "audit_id":   audit_id,
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "worker_id":  worker_id,
            **(data or {}),
        })
        return audit_id

    def log_approval_event(self, approval: "ApprovalRequest", event: str, by: str) -> None:
        """Record an approval lifecycle transition (created/approved/rejected/expired)."""
        self._append({
            "audit_id":    str(uuid.uuid4()),
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "event_type":  "approval_lifecycle",
            "event":       event,
            "approval_id": approval.approval_id,
            "worker_id":   approval.worker_id,
            "tool_name":   approval.tool_name,
            "resolved_by": by,
        })

    def _append(self, entry: dict) -> None:
        with open(self._path, "a") as f:
            f.write(json.dumps(entry) + "\n")
