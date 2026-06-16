"""
OpenWorker Memory Manager — runtime/memory_manager.py
=======================================================
Three distinct memory types, each with its own backend
(docs/CLAUDE_EXTENSION.md Decision 4):

  EPISODIC  — past task summaries per worker.    Backend: Supabase (table API)
  SEMANTIC  — company knowledge for RAG.          Backend: Postgres + pgvector (raw SQL)
  WORKING   — current task context, short-lived.  Backend: Redis, TTL 1 hour

Every method degrades gracefully:
  - No SUPABASE_URL/KEY or `supabase` not installed -> episodic falls
    back to an in-process list.
  - No DATABASE_URL or no embedding provider (OPENAI_API_KEY) configured
    -> semantic search/ingest returns empty / logs and no-ops.
  - No REDIS_URL or `redis` not installed -> working memory falls back
    to an in-process dict (lost on restart, but never blocks a task).

`load()` / `save()` are kept as the original episodic-only entry points
so core/agent_harness.py's system-prompt section [6] wiring doesn't
need to change.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("openworker.memory_manager")

_EPISODIC_TABLE = "worker_memory"
_EMBEDDING_DIM = 1536
_EMBEDDING_MODEL = "text-embedding-3-small"


class MemoryType(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    WORKING = "working"


class MemoryManager:
    """
    Usage:
        memory = MemoryManager(worker_id="OW-ARYAN-001", org_id="acme")
        recent = await memory.load(limit=10)                     # episodic, harness-facing
        await memory.save(task="...", output="...", cost_usd=0.01)

        chunks = await memory.load_semantic("pricing strategy", limit=5)
        await memory.ingest_knowledge("strategy/okrs-2025.pdf")

        ctx = await memory.load_working(task_id)
        await memory.save_working(task_id, {"step": 3})
    """

    def __init__(self, worker_id: str, org_id: str = "default"):
        self.worker_id = worker_id
        self.org_id = org_id

        # ── Episodic backend: Supabase table API ──
        self._supabase_url = os.environ.get("SUPABASE_URL")
        self._supabase_key = os.environ.get("SUPABASE_KEY")
        self._supabase: Any | None = None
        self._episodic_in_memory: list[dict] = []

        if self._supabase_url and self._supabase_key:
            try:
                from supabase import create_client

                self._supabase = create_client(self._supabase_url, self._supabase_key)
            except ImportError:
                logger.warning("supabase package not installed — episodic memory falls back to in-process storage")
            except Exception as exc:
                logger.warning("Could not initialise Supabase client: %s — episodic memory is in-process only", exc)

        # ── Semantic backend: Postgres + pgvector via raw SQL ──
        self._database_url = os.environ.get("DATABASE_URL")
        self._openai_key = os.environ.get("OPENAI_API_KEY")

        # ── Working backend: Redis ──
        self._redis_url = os.environ.get("REDIS_URL")
        self._redis: Any | None = None
        self._working_in_memory: dict[str, tuple[float, dict]] = {}  # task_id -> (expires_at, context)

        if self._redis_url:
            try:
                import redis.asyncio as redis_asyncio

                self._redis = redis_asyncio.from_url(self._redis_url, decode_responses=True)
            except ImportError:
                logger.warning("redis package not installed — working memory falls back to in-process storage")
            except Exception as exc:
                logger.warning("Could not initialise Redis client: %s — working memory is in-process only", exc)

    # ── Episodic — harness-facing aliases ──────────────────

    async def load(self, limit: int = 10) -> list[str]:
        """Backward-compatible entry point for AgentHarness system-prompt section [6]."""
        rows = await self.load_episodic(limit=limit)
        return [f"[{r['created_at']}] Task: {r['task']} -> {r['output_preview']}" for r in rows]

    async def save(self, task: str, output: str, cost_usd: float = 0.0, approval_id: str | None = None) -> None:
        """Backward-compatible entry point called by AgentHarness after a task finishes."""
        await self.save_episodic(task=task, output=output, cost_usd=cost_usd, approval_id=approval_id)

    # ── Episodic ────────────────────────────────────────────

    async def load_episodic(self, limit: int = 10) -> list[dict]:
        """Last `limit` task outcomes for this worker, oldest first."""
        if self._supabase is not None:
            try:
                response = (
                    self._supabase.table(_EPISODIC_TABLE)
                    .select("task, output_preview, created_at")
                    .eq("worker_id", self.worker_id)
                    .order("created_at", desc=True)
                    .limit(limit)
                    .execute()
                )
                return list(reversed(response.data or []))
            except Exception as exc:
                logger.warning("Supabase episodic load failed: %s — returning empty memory", exc)
                return []
        return self._episodic_in_memory[-limit:]

    async def save_episodic(
        self, task: str, output: str, cost_usd: float = 0.0, approval_id: str | None = None
    ) -> None:
        row = {
            "worker_id": self.worker_id,
            "task": task[:500],
            "output_preview": (output or "")[:500],
            "cost_usd": cost_usd,
            "approval_id": approval_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if self._supabase is not None:
            try:
                self._supabase.table(_EPISODIC_TABLE).insert(row).execute()
                return
            except Exception as exc:
                logger.warning("Supabase episodic save failed: %s — keeping in-process only", exc)
        self._episodic_in_memory.append(row)

    # ── Semantic (RAG) ──────────────────────────────────────

    async def load_semantic(self, query: str, limit: int = 5) -> list[str]:
        """
        Top-K knowledge_base chunks for this org, ranked by cosine similarity.
        Returns [] (and logs why) if Postgres or an embedding provider isn't
        configured — callers should treat that as "no extra context", not an error.
        """
        embedding = await self._embed(query)
        if embedding is None:
            return []

        conn = self._pg_connect()
        if conn is None:
            return []

        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT chunk_text FROM knowledge_base
                        WHERE org_id = %s AND (worker_id IS NULL OR worker_id = %s)
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                        """,
                        (self.org_id, self.worker_id, self._vector_literal(embedding), limit),
                    )
                    return [row[0] for row in cur.fetchall()]
        except Exception as exc:
            logger.warning("Semantic search failed: %s — returning no extra context", exc)
            return []
        finally:
            conn.close()

    async def ingest_knowledge(self, doc_path: str, worker_id: str | None = None, chunk_size: int = 1000) -> int:
        """
        Chunk a text/markdown file, embed each chunk, upsert into knowledge_base.
        Returns the number of chunks ingested (0 if not configured or the
        file/embedding provider isn't available — never raises).
        """
        conn = self._pg_connect()
        if conn is None:
            return 0
        try:
            text = open(doc_path, encoding="utf-8").read()
        except OSError as exc:
            logger.warning("Could not read %s for ingestion: %s", doc_path, exc)
            conn.close()
            return 0

        chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)] or []
        ingested = 0
        try:
            with conn:
                with conn.cursor() as cur:
                    for chunk in chunks:
                        embedding = await self._embed(chunk)
                        if embedding is None:
                            continue
                        cur.execute(
                            """
                            INSERT INTO knowledge_base (org_id, worker_id, doc_name, chunk_text, embedding)
                            VALUES (%s, %s, %s, %s, %s::vector)
                            """,
                            (self.org_id, worker_id, os.path.basename(doc_path), chunk, self._vector_literal(embedding)),
                        )
                        ingested += 1
        except Exception as exc:
            logger.warning("Knowledge ingestion failed for %s: %s — %d chunks committed before the error", doc_path, exc, ingested)
        finally:
            conn.close()
        return ingested

    async def _embed(self, text: str) -> list[float] | None:
        """OpenAI embeddings if OPENAI_API_KEY is set; else None (no embedding provider configured)."""
        if not self._openai_key:
            logger.info("No OPENAI_API_KEY configured — semantic memory is unavailable, skipping.")
            return None
        try:
            import openai

            client = openai.OpenAI(api_key=self._openai_key)
            response = client.embeddings.create(model=_EMBEDDING_MODEL, input=text)
            return response.data[0].embedding
        except ImportError:
            logger.warning("openai package not installed — semantic memory is unavailable")
            return None
        except Exception as exc:
            logger.warning("Embedding call failed: %s — semantic memory unavailable for this call", exc)
            return None

    def _pg_connect(self) -> Any | None:
        if not self._database_url:
            logger.info("No DATABASE_URL configured — semantic memory is unavailable, skipping.")
            return None
        try:
            import psycopg2

            return psycopg2.connect(self._database_url)
        except ImportError:
            logger.warning("psycopg2 not installed — semantic memory is unavailable")
            return None
        except Exception as exc:
            logger.warning("Postgres connection failed: %s — semantic memory unavailable for this call", exc)
            return None

    @staticmethod
    def _vector_literal(embedding: list[float]) -> str:
        return "[" + ",".join(str(v) for v in embedding) + "]"

    # ── Working ─────────────────────────────────────────────

    async def load_working(self, task_id: str) -> dict:
        """Current in-flight task context, or {} if none/expired."""
        if self._redis is not None:
            try:
                raw = await self._redis.get(f"working:{task_id}")
                if raw:
                    import json

                    return json.loads(raw)
                return {}
            except Exception as exc:
                logger.warning("Redis working-memory load failed: %s — returning empty context", exc)
                return {}

        entry = self._working_in_memory.get(task_id)
        if entry is None:
            return {}
        expires_at, context = entry
        if time.monotonic() > expires_at:
            del self._working_in_memory[task_id]
            return {}
        return context

    async def save_working(self, task_id: str, context: dict, ttl: int = 3600) -> None:
        """Store in-flight task context with a TTL (default 1 hour)."""
        if self._redis is not None:
            try:
                import json

                await self._redis.set(f"working:{task_id}", json.dumps(context), ex=ttl)
                return
            except Exception as exc:
                logger.warning("Redis working-memory save failed: %s — keeping in-process only", exc)

        self._working_in_memory[task_id] = (time.monotonic() + ttl, context)
