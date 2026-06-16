"""
OpenWorker Connector Registry — connectors/registry.py
=========================================================
Reads connectors.yaml and resolves a tool_name (as it appears in a
worker spec's tools.allowed / tools.approval_required) to a connector
instance. Falls back to a free/local provider when the primary isn't
configured, and to NullConnector (always reports clearly why) when
neither is.

connectors.yaml is keyed by *capability* (web_search, slack, documents,
...), not by individual tool name — several tool names can share one
capability. See connectors.yaml.example for the schema and
TOOL_NAME_TO_CAPABILITY below for the tool -> capability mapping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import yaml

from connectors.base import BaseConnector, NullConnector

logger = logging.getLogger("openworker.connector_registry")

# Worker-spec tool names that share one capability / connectors.yaml entry.
TOOL_NAME_TO_CAPABILITY: dict[str, str] = {
    "web_search": "web_search",
    "slack_post_dm": "slack",
    "slack_post_channel": "slack",
    "slack_read": "slack",
    "produce_document": "documents",
    "google_docs_create": "documents",
    "notion_create_page": "documents",
    "email_draft": "documents",
}

# Built-in defaults used when connectors.yaml has no entry for a capability
# at all (it's a company-supplied, gitignored file — must work without one).
# An entry present in connectors.yaml always overrides these.
_DEFAULT_ENTRIES: dict[str, dict] = {
    "web_search": {"provider": "brave", "fallback": "duckduckgo"},
    "slack": {"provider": "slack_bot", "fallback": "stdout"},
    "documents": {"provider": "local_file"},
}

# capability -> {provider_name: connector_class_loader}
# Loaders are callables so providers with optional/heavy imports (e.g. a
# future firecrawl or twilio connector) only import when actually selected.
_PROVIDERS: dict[str, dict[str, Callable[[], type[BaseConnector]]]] = {}


def register_provider(capability: str, provider_name: str, loader: Callable[[], type[BaseConnector]]) -> None:
    """Providers call this at import time to add themselves to the registry."""
    _PROVIDERS.setdefault(capability, {})[provider_name] = loader


class ConnectorRegistry:
    """
    Usage:
        registry = ConnectorRegistry("connectors.yaml")
        connector = registry.get_connector("web_search")
        if connector.is_configured():
            result = await connector.execute(payload)
    """

    DEFAULT_CONFIG_PATH = "connectors.yaml"

    def __init__(self, config_path: str | Path | None = None):
        self._config_path = Path(config_path or self.DEFAULT_CONFIG_PATH)
        self._config = self._load_config()
        self._ensure_providers_registered()

    def _load_config(self) -> dict:
        if not self._config_path.exists():
            logger.info(
                "%s not found — every capability will use its free/local fallback "
                "(or NullConnector if it has none). Copy connectors.yaml.example to get started.",
                self._config_path,
            )
            return {"connectors": {}}
        with open(self._config_path) as f:
            return yaml.safe_load(f) or {"connectors": {}}

    @staticmethod
    def _ensure_providers_registered() -> None:
        """Import provider modules so their register_provider() calls have run."""
        import connectors.providers.web_search.brave  # noqa: F401
        import connectors.providers.web_search.duckduckgo  # noqa: F401
        import connectors.providers.messaging.slack_bot  # noqa: F401
        import connectors.providers.documents.local_file  # noqa: F401

    def get_connector(self, tool_name: str) -> BaseConnector:
        """Resolve a worker-spec tool name to a configured connector, with fallback."""
        capability = TOOL_NAME_TO_CAPABILITY.get(tool_name, tool_name)
        entry = self._config.get("connectors", {}).get(capability) or _DEFAULT_ENTRIES.get(capability, {})

        primary_name = entry.get("provider")
        fallback_name = entry.get("fallback")

        primary = self._instantiate(capability, primary_name, entry)
        if primary is not None and primary.is_configured():
            return primary

        fallback = self._instantiate(capability, fallback_name, entry)
        if fallback is not None and fallback.is_configured():
            return fallback

        # Neither configured — prefer surfacing the primary's explanation
        # if one was named, else the fallback's, else a generic NullConnector.
        return primary or fallback or NullConnector(capability)

    def _instantiate(self, capability: str, provider_name: str | None, entry: dict) -> BaseConnector | None:
        if not provider_name:
            return None
        loader = _PROVIDERS.get(capability, {}).get(provider_name)
        if loader is None:
            logger.warning("No connector class registered for %s/%s", capability, provider_name)
            return None
        connector_cls = loader()
        return connector_cls(entry)
