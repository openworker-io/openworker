"""
DuckDuckGo connector — connectors/providers/web_search/duckduckgo.py
Free fallback for web_search. No API key needed, so is_configured() is
always True — it's the connector that catches everyone with no key set.
Limited to DuckDuckGo's instant-answer API (no full web results).
"""

from __future__ import annotations

import aiohttp

from connectors.base import BaseConnector, ConnectorResult
from connectors.registry import register_provider


class DuckDuckGoConnector(BaseConnector):
    """Always configured — the free, no-key fallback for web_search."""

    def __init__(self, config: dict | None = None):
        self._config = config or {}

    def is_configured(self) -> bool:
        return True

    def fallback_message(self) -> str:
        return "DuckDuckGo fallback is unavailable right now — try again shortly."

    async def execute(self, payload: dict) -> ConnectorResult:
        query = payload.get("query") or payload.get("action_summary", "")
        if not query:
            return ConnectorResult(success=False, error="web_search requires a 'query'.")

        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as resp:
                    if resp.status >= 400:
                        return ConnectorResult(success=False, error=f"DuckDuckGo returned HTTP {resp.status}")
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            return ConnectorResult(success=False, error=f"DuckDuckGo request failed: {exc}")

        lines = [f"Search: {query}", ""]
        abstract = data.get("AbstractText")
        if abstract:
            lines.append(f"Summary: {abstract}")
            lines.append(f"Source: {data.get('AbstractURL', '')}")
            lines.append("")

        related = data.get("RelatedTopics", [])[:5]
        for i, topic in enumerate(related, 1):
            text = topic.get("Text") if isinstance(topic, dict) else None
            href = topic.get("FirstURL") if isinstance(topic, dict) else None
            if text:
                lines.append(f"{i}. {text} ({href or 'no link'})")

        if not abstract and not related:
            lines.append(
                "No instant-answer results from DuckDuckGo's limited free API. "
                "Configure a BRAVE_API_KEY (or SerpAPI) in connectors.yaml for full web search results."
            )
        return ConnectorResult(success=True, output="\n".join(lines))


register_provider("web_search", "duckduckgo", lambda: DuckDuckGoConnector)
