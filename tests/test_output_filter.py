"""
Tests for core/output_filter.py — Harness Layer 4.
Unhappy paths first, per the OpenWorker testing approach.
"""

from pathlib import Path

import pytest

from core.output_filter import (
    REDACTED_CARD,
    REDACTED_EMAIL,
    REDACTED_SECRET,
    REDACTED_SSN,
    FilterResult,
    OutputFilter,
)
from core.permission_engine import WorkerSpec

FIXTURE = Path(__file__).parent / "fixtures" / "worker.test.yaml"


@pytest.fixture
def spec() -> WorkerSpec:
    return WorkerSpec(FIXTURE)


@pytest.fixture
def filt() -> OutputFilter:
    return OutputFilter()


# ─────────────────────────────────────────────
# UNHAPPY PATHS — PII scrubbing
# ─────────────────────────────────────────────

def test_credit_card_scrubbed(filt, spec):
    result = filt.filter("Card on file: 4111 1111 1111 1111 expires 09/27", spec)
    assert result.ok  # scrubbed, not blocked
    assert result.pii_detected
    assert REDACTED_CARD in result.scrubbed_output
    assert "4111" not in result.scrubbed_output
    assert "pii_card" in result.flags


def test_credit_card_with_dashes_scrubbed(filt, spec):
    result = filt.filter("Use 5500-0000-0000-0004 for the test", spec)
    assert REDACTED_CARD in result.scrubbed_output


def test_luhn_invalid_number_not_scrubbed(filt, spec):
    # 16 digits but fails Luhn — not a card, leave it alone
    result = filt.filter("Order ref 1234 5678 9012 3456 confirmed", spec)
    assert REDACTED_CARD not in result.scrubbed_output
    assert "pii_card" not in result.flags


def test_ssn_scrubbed(filt, spec):
    result = filt.filter("Their SSN is 123-45-6789.", spec)
    assert result.ok
    assert result.pii_detected
    assert REDACTED_SSN in result.scrubbed_output
    assert "123-45-6789" not in result.scrubbed_output


def test_unknown_email_scrubbed(filt, spec):
    result = filt.filter("Forward this to random.person@gmail.com today", spec)
    assert result.ok
    assert REDACTED_EMAIL in result.scrubbed_output
    assert "pii_email" in result.flags


def test_approver_email_allowlisted(filt, spec):
    # Fixture approver: test-manager@example.com — must pass through
    result = filt.filter("Escalated to test-manager@example.com for sign-off", spec)
    assert result.ok
    assert "test-manager@example.com" in result.scrubbed_output
    assert "pii_email" not in result.flags


def test_caller_allowlisted_email_kept(filt, spec):
    result = filt.filter(
        "Replied to client@customer.com as requested",
        spec,
        allowed_emails=["client@customer.com"],
    )
    assert "client@customer.com" in result.scrubbed_output
    assert "pii_email" not in result.flags


@pytest.mark.parametrize(
    "leak",
    [
        "Here is the key: sk-abc123def456ghi789jkl012",
        "Use AKIAIOSFODNN7EXAMPLE for S3",
        "token = abcdefghij1234567890XYZxyz",
        "Slack: xoxb-1234567890-abcdefghijk",
        "GitHub: ghp_abcdefghijklmnopqrstuv123456",
    ],
)
def test_secrets_scrubbed(filt, spec, leak):
    result = filt.filter(leak, spec)
    assert "secret_detected" in result.flags
    assert REDACTED_SECRET in result.scrubbed_output


# ─────────────────────────────────────────────
# UNHAPPY PATHS — blocking checks
# ─────────────────────────────────────────────

def test_content_policy_blocks(filt, spec):
    result = filt.filter("You should kill yourself over this.", spec)
    assert not result.ok
    assert result.content_flagged
    assert "content_policy" in result.flags


def test_invalid_json_blocked_when_spec_requires_json(filt, spec):
    spec._raw["behavior"] = {"output_format": "json"}
    result = filt.filter("This is not JSON at all", spec)
    assert not result.ok
    assert "format_invalid" in result.flags


def test_runaway_output_blocked(filt, spec):
    result = filt.filter("x" * 60_000, spec)
    assert not result.ok
    assert "output_too_long" in result.flags


# ─────────────────────────────────────────────
# HAPPY PATHS
# ─────────────────────────────────────────────

def test_clean_output_passes_unchanged(filt, spec):
    text = "Here is the LinkedIn draft you asked for. Three options included."
    result = filt.filter(text, spec)
    assert result.ok
    assert result.scrubbed_output == text
    assert not result.pii_detected
    assert not result.content_flagged
    assert result.flags == []


def test_valid_json_passes_when_required(filt, spec):
    spec._raw["behavior"] = {"output_format": "json"}
    result = filt.filter('{"status": "done", "items": [1, 2]}', spec)
    assert result.ok


def test_fenced_json_passes_when_required(filt, spec):
    spec._raw["behavior"] = {"output_format": "json"}
    result = filt.filter('```json\n{"status": "done"}\n```', spec)
    assert result.ok


def test_no_format_requirement_accepts_prose(filt, spec):
    result = filt.filter("Plain prose output", spec)
    assert result.ok


def test_returns_filter_result_type(filt, spec):
    assert isinstance(filt.filter("hello", spec), FilterResult)
