"""
OpenWorker Input Validator v0.1 — Harness Layer 1
==================================================
First gate for every inbound task. Runs BEFORE the task reaches the LLM.

Checks, in order:
  1. Sanitise        — strip null bytes / control chars, normalise whitespace
  2. Injection scan  — known prompt-injection patterns (on a normalised copy)
  3. Scope check     — task must not match the spec's out_of_scope list
  4. Length check    — max input length derived from the worker spec

Design notes:
- Sanitisation happens first so null-byte splitting can't evade the
  pattern scan ("ig\\x00nore previous instructions").
- The scan also runs on an NFKC-normalised, de-spaced copy to catch
  simple unicode / spacing evasion.
- The scope check in v0.1 is lexical only: it rejects tasks matching
  `role.out_of_scope` entries in the spec (if present) and otherwise
  passes with a flag. Semantic scope checking arrives with policy_rag.
- This layer never raises — it always returns a ValidationResult.
  The harness decides what to do with a rejection.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from core.constants import DEFAULT_MAX_INPUT_CHARS
from core.permission_engine import WorkerSpec

# Rough chars-per-token used to derive a char budget from
# model.max_context_tokens when the spec provides it.
_CHARS_PER_TOKEN = 4

# Control characters to strip (everything except \n and \t).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Collapse runs of spaces/tabs; newlines are preserved.
_SPACE_RUNS = re.compile(r"[ \t]{2,}")


@dataclass
class ValidationResult:
    """Returned by InputValidator.validate() for every inbound task."""
    ok: bool
    reason: str
    sanitised_task: str
    injection_detected: bool = False
    flags: list[str] = field(default_factory=list)


class InputValidator:
    """
    Layer 1 of the harness. Stateless — one instance can serve all workers.

    Usage:
        validator = InputValidator()
        result = validator.validate(task, worker_spec)
        if not result.ok:
            reject(result.reason)
    """

    INJECTION_PATTERNS = [
        "ignore previous instructions",
        "ignore your system prompt",
        "you are now",
        "pretend you are",
        "disregard your",
        "forget everything",
        "new instructions:",
        "####",
        "<|im_start|>",
        "<|im_end|>",
        "system:",        # raw role injection attempts
        "[INST]",         # Llama instruction format injection
    ]

    def validate(self, task: str, worker_spec: WorkerSpec) -> ValidationResult:
        """Run all Layer 1 checks. Never raises."""
        flags: list[str] = []

        # 1. Sanitise first — checks run on the cleaned text
        sanitised = self._sanitise(task)
        if sanitised != task:
            flags.append("input_sanitised")

        if not sanitised:
            return ValidationResult(
                ok=False,
                reason="Task is empty after sanitisation",
                sanitised_task=sanitised,
                flags=flags + ["empty_input"],
            )

        # 2. Injection scan
        pattern = self._scan_injection(sanitised)
        if pattern:
            return ValidationResult(
                ok=False,
                reason=f"Possible prompt injection detected: '{pattern}'",
                sanitised_task=sanitised,
                injection_detected=True,
                flags=flags + ["injection_pattern"],
            )

        # 3. Scope check (lexical, spec-driven)
        out_of_scope = self._scan_scope(sanitised, worker_spec)
        if out_of_scope:
            return ValidationResult(
                ok=False,
                reason=f"Task is outside this worker's scope: matched '{out_of_scope}'",
                sanitised_task=sanitised,
                flags=flags + ["out_of_scope"],
            )
        flags.append("scope_check_lexical_only")

        # 4. Length check
        max_chars = self._max_input_chars(worker_spec)
        if len(sanitised) > max_chars:
            return ValidationResult(
                ok=False,
                reason=f"Task length {len(sanitised)} exceeds max of {max_chars} chars",
                sanitised_task=sanitised,
                flags=flags + ["input_too_long"],
            )

        return ValidationResult(
            ok=True, reason="ok", sanitised_task=sanitised, flags=flags
        )

    # ── Checks ────────────────────────────────────────────

    def _sanitise(self, task: str) -> str:
        """Strip null bytes and control chars, collapse space runs, trim."""
        cleaned = _CONTROL_CHARS.sub("", task)
        cleaned = _SPACE_RUNS.sub(" ", cleaned)
        return cleaned.strip()

    def _scan_injection(self, task: str) -> str | None:
        """
        Return the first matched injection pattern, or None.
        Scans the lowercased text and an NFKC-normalised, de-spaced copy
        so 'I g n o r e previous…' and unicode lookalikes are caught.
        """
        lowered = task.lower()
        normalised = unicodedata.normalize("NFKC", lowered)
        despaced = re.sub(r"\s+", " ", normalised)
        compact = re.sub(r"\s+", "", normalised)
        for pattern in self.INJECTION_PATTERNS:
            p = pattern.lower()
            if p in lowered or p in despaced or p.replace(" ", "") in compact:
                return pattern
        return None

    def _scan_scope(self, task: str, worker_spec: WorkerSpec) -> str | None:
        """
        Lexical scope check: the spec may declare `role.out_of_scope` as a
        list of phrases this worker must never be tasked with. Returns the
        first match, or None. No list = no lexical restriction (v0.1).
        """
        phrases = worker_spec._raw.get("role", {}).get("out_of_scope", []) or []
        lowered = task.lower()
        for phrase in phrases:
            if str(phrase).lower() in lowered:
                return str(phrase)
        return None

    def _max_input_chars(self, worker_spec: WorkerSpec) -> int:
        """
        Char budget from the spec's model.max_context_tokens (≈4 chars/token),
        capped at the global default so a huge context window doesn't imply
        accepting huge untrusted input.
        """
        tokens = worker_spec._raw.get("model", {}).get("max_context_tokens")
        if tokens:
            return min(DEFAULT_MAX_INPUT_CHARS, int(tokens) * _CHARS_PER_TOKEN)
        return DEFAULT_MAX_INPUT_CHARS
