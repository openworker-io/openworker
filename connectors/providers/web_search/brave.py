"""Brave Search connector — connectors/providers/web_search/brave.py"""

from __future__ import annotations

import os

import aiohttp

from connectors.base import BaseConnector, ConnectorResult
from connectors.registry import register_provider


class BraveSearchConnector(BaseConnector):
    """Primary web_search provider. Needs BRAVE_API_KEY (or entry's api_key_env)."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        self._key_env = config.get("api_key_env", "BRAVE_API_KEY")

    def is_configured(self) -> bool:
        return bool(os.environ.get(self._key_env))

    def fallback_message(self) -> str:
        return f"Brave search requires {self._key_env} in .env."

    async def execute(self, payload: dict) -> ConnectorResult:
        api_key = os.environ.get(self._key_env, "")
        query = payload.get("query") or payload.get("action_summary", "")
        if not query:
            return ConnectorResult(success=False, error="web_search requires a 'query'.")

        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params={"q": query, "count": 5}) as resp:
                    if resp.status >= 400:
                        return ConnectorResult(success=False, error=f"Brave search failed: HTTP {resp.status}")
                    data = await resp.json()
        except aiohttp.ClientError as exc:
            return ConnectorResult(success=False, error=f"Brave search request failed: {exc}")

        results = data.get("web", {}).get("results", [])[:5]
        lines = [f"Search: {query}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '')}")
            if r.get("description"):
                lines.append(f"   {r['description']}")
            lines.append(f"   {r.get('url', '')}")
        if not results:
            lines.append("No results found.")
        return ConnectorResult(success=True, output="\n".join(lines))


register_provider("web_search", "brave", lambda: BraveSearchConnector)
