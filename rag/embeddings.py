"""Google AI embeddings client with caching, backoff and graceful degradation.

PROJECT_SPEC §5/§8: embeddings come from the Google AI API free tier
(``text-embedding-004``), with the key loaded from ``.env`` and **never**
hard-coded or logged (SECURITY_PLAN.md §2.1/§2.8).

Three things the free tier forces us to handle properly:

* **Missing key** — raise :class:`MissingApiKeyError` with setup guidance rather
  than failing deep inside an HTTP call with a confusing stack trace.
* **Quota (429)** — retry with exponential backoff *and jitter*, then raise
  :class:`QuotaExceededError`. Retrying a quota error forever just burns the
  next window too.
* **Re-embedding unchanged text** — a content-addressed cache keyed by
  ``(model, sha256(text))`` means re-running a campaign over unedited playbooks
  costs zero API calls.

The network client is injected, so every test in this repo runs offline against
a fake that returns canned vectors.
"""
from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_cache (
    cache_key  TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    created_at TEXT
);
"""

# Google's embed endpoint caps how many texts one request may carry.
DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY_S = 1.0
DEFAULT_MAX_DELAY_S = 60.0

# Retrieval quality improves when the query and the corpus are embedded with
# different task types — Google's models are trained for this asymmetry.
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"


class EmbeddingError(Exception):
    """Base class for embedding failures."""


class MissingApiKeyError(EmbeddingError):
    """No Google API key configured."""


class QuotaExceededError(EmbeddingError):
    """Free-tier quota exhausted after exhausting retries."""


class EmbeddingClient(Protocol):
    """The seam a different embeddings provider would implement."""

    model: str

    def embed(self, texts: Sequence[str], *, task_type: str) -> List[List[float]]: ...


def _is_quota_error(exc: BaseException) -> bool:
    """Whether an exception looks like a rate-limit/quota rejection.

    Matched structurally where possible (``status_code``/``code``) and by
    message only as a fallback, since the SDK's exception types vary by version.
    """
    for attr in ("status_code", "code", "status"):
        value = getattr(exc, attr, None)
        if value == 429 or str(value) == "429":
            return True
        if isinstance(value, str) and value.upper() in {
            "RESOURCE_EXHAUSTED", "TOO_MANY_REQUESTS"
        }:
            return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        marker in text
        for marker in ("429", "resource_exhausted", "rate limit", "quota", "too many requests")
    )


def backoff_delays(
    attempts: int,
    *,
    base: float = DEFAULT_BASE_DELAY_S,
    cap: float = DEFAULT_MAX_DELAY_S,
    jitter: Callable[[], float] = random.random,
) -> List[float]:
    """Exponential backoff delays with full jitter, capped.

    Jitter is injectable so tests assert the schedule deterministically.
    """
    delays = []
    for attempt in range(attempts):
        window = min(cap, base * (2 ** attempt))
        delays.append(round(window * jitter(), 6))
    return delays


def resolve_api_key(explicit: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> str:
    """Find the Google API key, or explain how to set one.

    The key itself is never included in the error message (SECURITY_PLAN §2.8).
    """
    environ = os.environ if env is None else env
    key = explicit or environ.get("GOOGLE_API_KEY") or ""
    key = key.strip()
    if not key:
        raise MissingApiKeyError(
            "GOOGLE_API_KEY is not set. Copy .env.example to .env and add your "
            "Google AI API key (free tier). The key is read from the environment "
            "and must never be committed."
        )
    return key


def cache_key(model: str, text: str) -> str:
    """Content-addressed cache key: identical text under one model hits."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{model}:{digest}"


class EmbeddingCache:
    """SQLite-backed cache so unchanged text is never re-embedded."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        conn.executescript(CACHE_SCHEMA)
        conn.commit()

    def get_many(self, model: str, texts: Sequence[str]) -> Dict[str, List[float]]:
        """Return ``{text: vector}`` for whichever texts are already cached."""
        import numpy as np

        if not texts:
            return {}
        keys = {cache_key(model, t): t for t in texts}
        placeholders = ", ".join("?" for _ in keys)
        rows = self._conn.execute(
            f"SELECT cache_key, dim, vector FROM embedding_cache WHERE cache_key IN ({placeholders})",
            list(keys),
        ).fetchall()
        out: Dict[str, List[float]] = {}
        for row in rows:
            text = keys.get(row["cache_key"])
            if text is None:
                continue
            out[text] = np.frombuffer(row["vector"], dtype=np.float32).tolist()
        return out

    def put_many(self, model: str, pairs: Sequence[tuple], *, created_at: Optional[str] = None) -> int:
        """Store ``[(text, vector), ...]``, stamped with the write time.

        No caller ever passed ``created_at``, so every row carried NULL and the
        cache could not be aged out — the one thing the column is for. It
        defaults to now rather than being dropped because a content-addressed
        cache with no write time can only be cleared wholesale.
        """
        import numpy as np

        if not pairs:
            return 0
        if created_at is None:
            created_at = (
                datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        rows = [
            (
                cache_key(model, text), model, len(vector),
                np.asarray(vector, dtype=np.float32).tobytes(), created_at,
            )
            for text, vector in pairs
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO embedding_cache"
                " (cache_key, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) AS n FROM embedding_cache").fetchone()["n"])


class GoogleEmbeddingClient:
    """Google AI embeddings with batching, retry/backoff and optional caching.

    ``transport`` is the injection point: a callable
    ``(texts, model, task_type) -> list[list[float]]``. When omitted, a real
    ``google-genai`` client is built lazily so importing this module never
    requires the SDK (or a key) to be present.
    """

    def __init__(
        self,
        *,
        model: str = "text-embedding-004",
        api_key: Optional[str] = None,
        transport: Optional[Callable[[Sequence[str], str, str], List[List[float]]]] = None,
        cache: Optional[EmbeddingCache] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.model = model
        self._explicit_key = api_key
        self._transport = transport
        self._cache = cache
        self._batch_size = max(1, batch_size)
        self._max_retries = max(0, max_retries)
        self._sleep = sleep
        self._jitter = jitter

    # -- transport --------------------------------------------------------- #
    def _build_default_transport(self):
        """Lazily construct a real google-genai transport."""
        key = resolve_api_key(self._explicit_key)
        try:
            from google import genai  # imported lazily: tests never need the SDK
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise EmbeddingError(
                "google-genai is not installed. Install it with "
                "`pip install -r requirements.txt` to use live embeddings."
            ) from exc

        client = genai.Client(api_key=key)

        def transport(texts, model, task_type):
            response = client.models.embed_content(
                model=model,
                contents=list(texts),
                config={"task_type": task_type},
            )
            return [list(e.values) for e in response.embeddings]

        return transport

    def _call(self, texts: Sequence[str], task_type: str) -> List[List[float]]:
        """One transport call wrapped in retry/backoff on quota errors."""
        if self._transport is None:
            self._transport = self._build_default_transport()

        delays = backoff_delays(self._max_retries, jitter=self._jitter)
        last_exc: Optional[BaseException] = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._transport(texts, self.model, task_type)
            except Exception as exc:
                if not _is_quota_error(exc):
                    raise
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                self._sleep(delays[attempt])

        raise QuotaExceededError(
            f"Google AI quota exhausted after {self._max_retries} retries. "
            "The free tier limits requests per minute; wait for the window to "
            "reset or reduce the corpus size."
        ) from last_exc

    # -- public API -------------------------------------------------------- #
    def embed(
        self, texts: Sequence[str], *, task_type: str = TASK_DOCUMENT
    ) -> List[List[float]]:
        """Embed texts, in cache-then-batch order, preserving input order."""
        texts = list(texts)
        if not texts:
            return []

        cached: Dict[str, List[float]] = {}
        if self._cache is not None:
            cached = self._cache.get_many(self.model, texts)

        # Deduplicate within the request too: the same chunk twice costs one call.
        pending = []
        for text in texts:
            if text not in cached and text not in pending:
                pending.append(text)

        fresh: Dict[str, List[float]] = {}
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start:start + self._batch_size]
            vectors = self._call(batch, task_type)
            if len(vectors) != len(batch):
                raise EmbeddingError(
                    f"Embedding API returned {len(vectors)} vectors for {len(batch)} inputs"
                )
            fresh.update(dict(zip(batch, vectors)))

        if self._cache is not None and fresh:
            self._cache.put_many(self.model, list(fresh.items()))

        merged = {**cached, **fresh}
        return [list(merged[text]) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        """Embed a single retrieval *query* (asymmetric task type)."""
        return self.embed([text], task_type=TASK_QUERY)[0]

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed corpus *documents* (asymmetric task type)."""
        return self.embed(texts, task_type=TASK_DOCUMENT)
