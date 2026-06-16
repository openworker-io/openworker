"""Tests for core/permission_engine.py"""

import json
import os
import tempfile

import pytest
import yaml

from core.permission_engine import (
    AuditLogger,
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    PermissionEngine,
    WorkerSpec,
    WorkerStatus,
)

TEST_SPEC = "tests/fixtures/worker.test.yaml"


@pytest.fixture
def spec() -> WorkerSpec:
    return WorkerSpec(TEST_SPEC)


@pytest.fixture
def tmp_audit_path(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def engine(spec, tmp_audit_path) -> PermissionEngine:
    return PermissionEngine(spec, audit_logger=AuditLogger(tmp_audit_path))


def _load_raw(path: str = TEST_SPEC) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class TestWorkerSpec:
    def test_loads_valid_spec(self, spec):
        assert spec.worker_name == "TestWorker"
        assert spec.worker_id == "OW-TEST-001"
        assert spec.worker_status == WorkerStatus.ACTIVE
        assert spec.autonomy_level == "L2"

    def test_rejects_missing_required_fields(self):
        raw = _load_raw()
        del raw["tools"]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(raw, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="missing required field"):
                WorkerSpec(path)
        finally:
            os.unlink(path)

    def test_absolute_blocks_cannot_be_in_allowed(self):
        raw = _load_raw()
        raw["tools"]["allowed"].append({"name": "payroll_systems"})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(raw, f)
            path = f.name
        try:
            tampered = WorkerSpec(path)
            assert tampered.classify("payroll_systems") == Decision.BLOCKED
        finally:
            os.unlink(path)

    def test_classify_allowed_tool(self, spec):
        assert spec.classify("web_search") == Decision.ALLOWED

    def test_classify_approval_tool(self, spec):
        assert spec.classify("email_send") == Decision.APPROVAL

    def test_classify_blocked_tool(self, spec):
        assert spec.classify("payroll_systems") == Decision.BLOCKED

    def test_classify_unknown_tool_returns_unknown(self, spec):
        assert spec.classify("some_tool_not_in_spec") == Decision.UNKNOWN


class TestPermissionEngine:
    def test_blocked_decision_for_payroll(self, engine):
        result = engine.check("payroll_systems")
        assert result.decision == Decision.BLOCKED
        assert result.is_hard_stop()

    def test_blocked_decision_for_hr_systems(self, engine):
        result = engine.check("hr_systems")
        assert result.decision == Decision.BLOCKED

    def test_blocked_decision_for_production_db(self, engine):
        result = engine.check("production_databases")
        assert result.decision == Decision.BLOCKED

    def test_allowed_decision_for_web_search(self, engine):
        result = engine.check("web_search", {"action_summary": "Search for X"})
        assert result.decision == Decision.ALLOWED
        assert result.is_executable()

    def test_approval_decision_creates_approval_request(self, engine):
        result = engine.check("email_send", {"action_summary": "Send a reply"})
        assert result.decision == Decision.APPROVAL
        assert result.requires_approval()
        assert result.approval_id is not None
        assert result.approver_email == "test-manager@example.com"

    def test_unknown_tool_is_hard_stop(self, engine):
        result = engine.check("totally_unknown_tool")
        assert result.decision == Decision.UNKNOWN
        assert result.is_hard_stop()

    def test_suspended_worker_blocks_all_tools(self, tmp_audit_path):
        raw = _load_raw()
        raw["identity"]["status"] = "suspended"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(raw, f)
            path = f.name
        try:
            suspended_spec = WorkerSpec(path)
            suspended_engine = PermissionEngine(suspended_spec, audit_logger=AuditLogger(tmp_audit_path))
            result = suspended_engine.check("web_search")
            assert result.decision == Decision.BLOCKED
            assert "suspended" in result.reason
        finally:
            os.unlink(path)

    def test_trust_score_increases_on_approval(self, engine, spec):
        before = spec._raw["autonomy"]["trust_score"]
        result = engine.check("email_send", {"action_summary": "Send a reply"})
        engine.resolve_approval(result.approval_id, ApprovalStatus.APPROVED, resolved_by="manager@example.com")
        after = spec._raw["autonomy"]["trust_score"]
        assert after > before

    def test_trust_score_decreases_on_rejection(self, engine, spec):
        before = spec._raw["autonomy"]["trust_score"]
        result = engine.check("email_send", {"action_summary": "Send a reply"})
        engine.resolve_approval(result.approval_id, ApprovalStatus.REJECTED, resolved_by="manager@example.com")
        after = spec._raw["autonomy"]["trust_score"]
        assert after < before

    def test_audit_log_written_for_every_decision(self, engine, tmp_audit_path):
        engine.check("web_search")
        engine.check("payroll_systems")
        assert tmp_audit_path.exists()
        lines = tmp_audit_path.read_text().strip().splitlines()
        assert len(lines) == 2
        entries = [json.loads(line) for line in lines]
        assert entries[0]["decision"] == "allowed"
        assert entries[1]["decision"] == "blocked"

    def test_approval_request_slack_message_format(self):
        approval = ApprovalRequest(
            worker_name="TestWorker",
            tool_name="email_send",
            action_summary="Send a reply to the customer",
            content_preview="Hi there, thanks for reaching out...",
        )
        message = approval.to_slack_message()
        assert "blocks" in message
        header = message["blocks"][0]
        assert header["type"] == "header"
        assert "TestWorker" in header["text"]["text"]
        actions = message["blocks"][-1]["elements"]
        values = [el["value"] for el in actions]
        assert f"approve:{approval.approval_id}" in values
        assert f"reject:{approval.approval_id}" in values
