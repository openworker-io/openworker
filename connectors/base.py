"""
OpenWorker Connector Base — connectors/base.py
=================================================
The interface every connector implements. A connector wraps exactly
one external provider for one capability (e.g. "Brave for web search",
"Slack bot token for messaging"). Connectors are stateless beyond
config read from os.environ at call time (constraint 12 in
docs/CLAUDE_EXTENSION.md) — never cache credentials as instance state
beyond what one execute() call needs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConnectorResult:
    """Returned by every connector's execute()."""
    success: bool
    output: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseConnector(ABC):
    """One provider for one capability. See connectors/providers/ for implementations."""

    @abstractmethod
    async def execute(self, payload: dict) -> ConnectorResult:
        """Run the call. Should not raise — caught failures come back as ConnectorResult(success=False, ...)."""

    @abstractmethod
    def is_configured(self) -> bool:
        """True if the required env vars are present. Checked before execute() is called."""

    @abstractmethod
    def fallback_message(self) -> str:
        """Human-readable explanation shown when this connector isn't configured."""


class NullConnector(BaseConnector):
    """
    Returned by the registry when neither the primary nor the fallback
    provider for a capability is configured. Always reports
    not-configured rather than raising — fail safe, not fail loud.
    """

    def __init__(self, capability: str):
        self.capability = capability

    async def execute(self, payload: dict) -> ConnectorResult:
        return ConnectorResult(success=False, error=self.fallback_message())

    def is_configured(self) -> bool:
        return False

    def fallback_message(self) -> str:
        return (
            f"No connector configured for '{self.capability}'. "
            f"Add a provider in connectors.yaml and set its API key in .env."
        )
