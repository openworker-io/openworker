"""
OpenWorker Output Filter v0.1 — Harness Layer 4
================================================
Last gate before any worker output leaves the harness. Runs on the
final LLM text BEFORE it is returned, sent, or stored.

Checks, in order:
  1. PII scrub      — credit cards (Luhn-validated), SSNs, emails,
                      API keys / secrets. Scrubbed, not blocked.
  2. Content policy — violence / self-harm phrases. Blocked.
  3. Format check   — if the spec demands JSON, the output must parse.
  4. Length check   — runaway outputs are blocked.

Design notes:
- PII is SCRUBBED (output continues with redaction markers); policy,
  format, and length violations BLOCK (ok=False). Fail safe.
- Emails are scrubbed unless allowlisted: addresses from the worker's
  own org chart (approver, backup) and any passed via `allowed_emails`
  (e.g. addresses that appeared in the inbound task) are kept.
- JSON format enforcement is opt-in via `behavior.output_format: json`
  in the worker spec. No field = no format constraint.
- This layer never raises — it always returns a FilterResult.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from core.constants import DEFAULT_MAX_OUTPUT_CHARS
from core.permission_engine import WorkerSpec

# ── Redaction markers ────────────────────────────────────

REDACTED_CARD = "[REDACTED:CARD]"
REDACTED_SSN = "[REDACTED:SSN]"
REDACTED_EMAIL = "[REDACTED:EMAIL]"
REDACTED_SECRET = "[REDACTED:SECRET]"

# ── Detection patterns ───────────────────────────────────

# 13–19 digits, optionally separated by spaces or dashes (no trailing
# separator in the match). Candidates are confirmed with a Luhn check
# before scrubbing.
_CARD_CANDIDATE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")

# US SSN — dashes required, to keep false positives down.
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Common secret/key shapes: Anthropic/OpenAI/Slack/GitHub keys, AWS
# access key IDs, and generic long bearer tokens after 'key'/'token'.
_SECRETS = re.compile(
    r"""(
        sk-[A-Za-z0-9_-]{16,}            |
        xox[abprs]-[A-Za-z0-9-]{10,}     |
        gh[pousr]_[A-Za-z0-9]{20,}       |
        AKIA[0-9A-Z]{16}                 |
        (?:api[_-]?key|token|secret)\s*[:=]\s*["']?[A-Za-z0-9_\-./+]{20,}["']?
    )""",
    re.VERBOSE | re.IGNORECASE,
)

# Lexical content-policy phrases (v0.1). Word-boundary matched.
_POLICY_PHRASES = [
    "kill yourself",
    "how to make a bomb",
    "how to build a bomb",
    "harm yourself",
    "commit suicide",
]


@dataclass
class FilterResult:
    """Returned by OutputFilter.filter() for every worker output."""
    ok: bool
    scrubbed_output: str
    pii_detected: bool = False
    content_flagged: bool = False
    flags: list[str] = field(default_factory=list)


class OutputFilter:
    """
    Layer 4 of the harness. Stateless — one instance can serve all workers.

    Usage:
        filt = OutputFilter()
        result = filt.filter(llm_output, worker_spec)
        if result.ok:
            deliver(result.scrubbed_output)
    """

    def filter(
        self,
        output: str,
        worker_spec: WorkerSpec,
        allowed_emails: Iterable[str] | None = None,
    ) -> FilterResult:
        """
        Run all Layer 4 checks. Never raises.

        Args:
            output:         the final LLM text
            worker_spec:    the worker's spec (allowlists, format rules)
            allowed_emails: extra addresses that may pass through unscrubbed
                            (e.g. emails that appeared in the inbound task)
        """
        flags: list[str] = []

        # 1. PII + secret scrub (output continues, redacted)
        scrubbed, pii_flags = self._scrub_pii(
            output, self._email_allowlist(worker_spec, allowed_emails)
        )
        flags.extend(pii_flags)
        pii_detected = bool(pii_flags)

        # 2. Content policy (blocks)
        policy_hit = self._scan_policy(scrubbed)
        if policy_hit:
            return FilterResult(
                ok=False,
                scrubbed_output=scrubbed,
                pii_detected=pii_detected,
                content_flagged=True,
                flags=flags + ["content_policy"],
            )

        # 3. Format enforcement (blocks)
        if not self._check_format(scrubbed, worker_spec):
            return FilterResult(
                ok=False,
                scrubbed_output=scrubbed,
                pii_detected=pii_detected,
                flags=flags + ["format_invalid"],
            )

        # 4. Length check (blocks)
        if len(scrubbed) > DEFAULT_MAX_OUTPUT_CHARS:
            return FilterResult(
                ok=False,
                scrubbed_output=scrubbed,
                pii_detected=pii_detected,
                flags=flags + ["output_too_long"],
            )

        return FilterResult(
            ok=True,
            scrubbed_output=scrubbed,
            pii_detected=pii_detected,
            flags=flags,
        )

    # ── 1. PII / secrets ──────────────────────────────────

    def _scrub_pii(self, text: str, email_allowlist: set[str]) -> tuple[str, list[str]]:
        """Replace PII and secrets with redaction markers. Returns (text, flags)."""
        flags: list[str] = []

        def _card_sub(match: re.Match) -> str:
            digits = re.sub(r"\D", "", match.group())
            if 13 <= len(digits) <= 19 and self._luhn_ok(digits):
                if "pii_card" not in flags:
                    flags.append("pii_card")
                return REDACTED_CARD
            return match.group()  # not a real card number — leave it

        text = _CARD_CANDIDATE.sub(_card_sub, text)

        if _SSN.search(text):
            flags.append("pii_ssn")
            text = _SSN.sub(REDACTED_SSN, text)

        def _email_sub(match: re.Match) -> str:
            if match.group().lower() in email_allowlist:
                return match.group()
            if "pii_email" not in flags:
                flags.append("pii_email")
            return REDACTED_EMAIL

        text = _EMAIL.sub(_email_sub, text)

        if _SECRETS.search(text):
            flags.append("secret_detected")
            text = _SECRETS.sub(REDACTED_SECRET, text)

        return text, flags

    @staticmethod
    def _luhn_ok(digits: str) -> bool:
        """Luhn checksum — filters out phone numbers and random digit runs."""
        total = 0
        for i, ch in enumerate(reversed(digits)):
            d = int(ch)
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        return total % 10 == 0

    def _email_allowlist(
        self, worker_spec: WorkerSpec, extra: Iterable[str] | None
    ) -> set[str]:
        """Org-chart addresses + caller-provided extras may pass unscrubbed."""
        allowed = {e.lower() for e in (extra or [])}
        org = worker_spec._raw.get("org", {})
        for person in (org.get("reports_to"), org.get("backup_approver")):
            if person and person.get("email"):
                allowed.add(person["email"].lower())
        return allowed

    # ── 2. Content policy ─────────────────────────────────

    def _scan_policy(self, text: str) -> str | None:
        """Return the first matched policy phrase, or None. Lexical in v0.1."""
        lowered = text.lower()
        for phrase in _POLICY_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                return phrase
        return None

    # ── 3. Format enforcement ─────────────────────────────

    def _check_format(self, text: str, worker_spec: WorkerSpec) -> bool:
        """
        If the spec sets behavior.output_format: json, the output must be
        valid JSON (markdown code fences are tolerated and stripped).
        """
        required = worker_spec._raw.get("behavior", {}).get("output_format")
        if required != "json":
            return True
        candidate = text.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", candidate, re.DOTALL)
        if fence:
            candidate = fence.group(1)
        try:
            json.loads(candidate)
            return True
        except (json.JSONDecodeError, ValueError):
            return False
