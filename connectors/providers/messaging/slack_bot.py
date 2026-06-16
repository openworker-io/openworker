"""
Slack connector — connectors/providers/messaging/slack_bot.py
Handles slack_post_dm / slack_post_channel / slack_read tool calls.
Supports a bot token or an incoming webhook; is_configured() is False
for either when no credentials are present, so the registry falls
through to the registry's configured fallback (stdout) automatically.
"""

from __future__ import annotations

import os

import aiohttp

from connectors.base import BaseConnector, ConnectorResult
from connectors.registry import register_provider


class SlackConnector(BaseConnector):
    def __init__(self, config: dict | None = None):
        config = config or {}
        self._token_env = config.get("bot_token_env", "SLACK_BOT_TOKEN")
        self._webhook_env = config.get("webhook_env", "SLACK_WEBHOOK_URL")

    def is_configured(self) -> bool:
        return bool(os.environ.get(self._token_env) or os.environ.get(self._webhook_env))

    def fallback_message(self) -> str:
        return f"Slack requires {self._token_env} or {self._webhook_env} in .env."

    async def execute(self, payload: dict) -> ConnectorResult:
        action = payload.get("action", "post_dm")
        if action == "read":
            return await self._read(payload)
        return await self._post(payload)

    async def _post(self, payload: dict) -> ConnectorResult:
        recipient = payload.get("recipient") or payload.get("channel", "#general")
        message = payload.get("message") or payload.get("content_preview", "") or payload.get("action_summary", "")
        token = os.environ.get(self._token_env)
        webhook = os.environ.get(self._webhook_env)

        try:
            async with aiohttp.ClientSession() as session:
                if webhook:
                    async with session.post(webhook, json={"text": f"To {recipient}: {message}"}) as resp:
                        ok = resp.status == 200
                else:
                    async with session.post(
                        "https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"channel": recipient, "text": message},
                    ) as resp:
                        data = await resp.json()
                        ok = bool(data.get("ok", False))
        except aiohttp.ClientError as exc:
            return ConnectorResult(success=False, error=f"Slack request failed: {exc}")

        if ok:
            return ConnectorResult(success=True, output=f"Slack message sent to {recipient}")
        return ConnectorResult(success=False, error="Slack API call did not confirm delivery")

    async def _read(self, payload: dict) -> ConnectorResult:
        channel = payload.get("channel", "#general")
        limit = int(payload.get("limit", 10))
        token = os.environ.get(self._token_env)
        if not token:
            return ConnectorResult(success=False, error=f"slack_read requires {self._token_env}.")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://slack.com/api/conversations.history",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"channel": channel, "limit": limit},
                ) as resp:
                    data = await resp.json()
        except aiohttp.ClientError as exc:
            return ConnectorResult(success=False, error=f"Slack read failed: {exc}")

        if not data.get("ok", False):
            return ConnectorResult(success=False, error=f"Slack read failed: {data.get('error', 'unknown error')}")
        messages = [m.get("text", "") for m in data.get("messages", [])]
        return ConnectorResult(success=True, output="\n".join(messages) or "(no messages)")


class StdoutSlackConnector(BaseConnector):
    """
    Always-configured fallback for the 'slack' capability. Logs the
    message clearly instead of failing when no Slack credentials exist —
    the demo must work with no Slack configuration at all.
    """

    def __init__(self, config: dict | None = None):
        self._config = config or {}

    def is_configured(self) -> bool:
        return True

    def fallback_message(self) -> str:
        return "stdout fallback is always available."

    async def execute(self, payload: dict) -> ConnectorResult:
        if payload.get("action") == "read":
            return ConnectorResult(success=True, output="[SLACK READ] no Slack configured — no messages available")
        recipient = payload.get("recipient") or payload.get("channel", "#general")
        message = payload.get("message") or payload.get("content_preview", "") or payload.get("action_summary", "")
        line = f"[SLACK -> {recipient}]: {message}"
        return ConnectorResult(success=True, output=line)


register_provider("slack", "slack_bot", lambda: SlackConnector)
register_provider("slack", "stdout", lambda: StdoutSlackConnector)
