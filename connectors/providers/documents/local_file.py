"""
Local file connector — connectors/providers/documents/local_file.py
Backs produce_document / google_docs_create / notion_create_page /
email_draft until real Google Docs / Notion / email API integrations
land. Always configured — writing to the sandboxed workspace needs no
external credentials. Files never leave /tmp/worker-workspace/{task_id}/.
"""

from __future__ import annotations

import re
from pathlib import Path

from connectors.base import BaseConnector, ConnectorResult
from connectors.registry import register_provider

WORKSPACE_ROOT = Path("/tmp/worker-workspace")


class LocalFileConnector(BaseConnector):
    def __init__(self, config: dict | None = None):
        self._config = config or {}

    def is_configured(self) -> bool:
        return True

    def fallback_message(self) -> str:
        return "Local file fallback is always available."

    async def execute(self, payload: dict) -> ConnectorResult:
        task_id = payload.get("task_id", "adhoc")
        workspace = WORKSPACE_ROOT / task_id
        workspace.mkdir(parents=True, exist_ok=True)

        if payload.get("kind") == "email_draft":
            return self._write_email_draft(workspace, payload)
        return self._write_document(workspace, payload)

    def _write_document(self, workspace: Path, payload: dict) -> ConnectorResult:
        title = payload.get("title") or payload.get("action_summary", "Untitled document")
        content = payload.get("content") or payload.get("content_preview", "")
        if not content:
            return ConnectorResult(success=False, error="Document creation requires a 'content' argument.")

        path = workspace / (self._slugify(title) + ".md")
        path.write_text(f"# {title}\n\n{content}\n")
        preview = content[:300] + ("..." if len(content) > 300 else "")
        return ConnectorResult(success=True, output=f"Document written to {path}\n\nPreview:\n{preview}")

    def _write_email_draft(self, workspace: Path, payload: dict) -> ConnectorResult:
        to = payload.get("to") or "unspecified-recipient"
        subject = payload.get("subject") or payload.get("action_summary", "No subject")
        body = payload.get("body") or payload.get("content_preview", "")

        path = workspace / "draft_email.md"
        path.write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n")
        preview = body[:300] + ("..." if len(body) > 300 else "")
        return ConnectorResult(
            success=True,
            output=f"Draft saved to {path} (not sent — sending requires approval)\n\nPreview:\n{preview}",
        )

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug or "document"


register_provider("documents", "local_file", lambda: LocalFileConnector)
