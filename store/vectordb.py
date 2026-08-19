"""Vector store for RAG retrieval — embeddings in SQLite, exact search in numpy.

Replaces ChromaDB (see ``docs/PROJECT_SPEC.md`` §8, decision #6). Embeddings
live as ``float32`` BLOBs in the *same* SQLite database as the runs, so a run
and its embedding commit in one transaction — no dual-store consistency
problem, one file to back up.

**Why exact search, not an ANN index.** The project's core requirement is a
reproducible report (§6.2): identical inputs must retrieve identical context.
Exact cosine top-k guarantees that. An approximate index can return different
neighbours after a rebuild or a parameter change, which would let "same data →
same report" break silently and invisibly.

**Why this is fast enough.** Retrieval is one matrix multiply against a corpus
of knowledge-base playbook chunks plus prior findings. At 768 dimensions,
100k chunks is ~300 MB and ~50 ms per query — far beyond the expected corpus.
Vectors are L2-normalised on write, so cosine similarity *is* the dot product
and the query is a single ``matrix @ vector``.

**Why the corpus is cached per store instance.** Analysis retrieves once per
page — twice with ``--use-priors`` — and each query used to re-read every row
and rebuild the whole matrix, so a ten-page campaign loaded the corpus twenty
times. The cache is keyed by query scope and dropped by every write, and it
cannot go stale between queries because every write goes through this object.

If the corpus ever outgrows this, :class:`VectorStore` is the seam: implement
it over LanceDB (embedded, ANN) without touching the ``rag/`` layer.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence

import numpy as np

# float32 keeps the store half the size of float64 at no meaningful recall cost.
DTYPE = np.float32

VECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    doc_id     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    source     TEXT,
    metadata   TEXT NOT NULL DEFAULT '{}',
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_embeddings_kind ON embeddings (kind);
CREATE INDEX IF NOT EXISTS idx_embeddings_source ON embeddings (source);
"""


class VectorStoreError(Exception):
    """User-facing error for vector store failures."""


@dataclass(frozen=True)
class Document:
    """A chunk of text to embed and retrieve.

    ``kind`` separates corpora that are retrieved differently — e.g.
    ``knowledge`` (curated playbooks) vs ``finding`` (prior run findings).
    """

    doc_id: str
    text: str
    kind: str = "knowledge"
    source: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchHit:
    """One retrieval result, ordered by descending ``score`` (cosine, -1..1)."""

    doc_id: str
    text: str
    kind: str
    source: Optional[str]
    metadata: Dict[str, Any]
    score: float


class VectorStore(Protocol):
    """The seam a different backend (e.g. LanceDB) would implement."""

    def add(self, documents: Sequence[Document], vectors: Any, *, model: str) -> int: ...

    def query(
        self, vector: Any, *, k: int = 5, kind: Optional[str] = None
    ) -> List[SearchHit]: ...

    def delete(self, *, doc_ids: Optional[Sequence[str]] = None,
               kind: Optional[str] = None) -> int: ...

    def count(self, *, kind: Optional[str] = None) -> int: ...


# --------------------------------------------------------------------------- #
# vector helpers (pure, unit-testable without a database)
# --------------------------------------------------------------------------- #
def normalize(vectors: Any) -> np.ndarray:
    """L2-normalise row-wise so cosine similarity reduces to a dot product.

    Zero vectors are left as zeros rather than producing NaN — they simply
    score 0 against everything, which is the correct "no signal" behaviour.
    """
    array = np.asarray(vectors, dtype=DTYPE)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.ndim != 2:
        raise VectorStoreError(f"Expected 1-D or 2-D vectors, got {array.ndim}-D")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (array / norms).astype(DTYPE)


def to_blob(vector: Any) -> bytes:
    """Serialise one vector to bytes for storage."""
    return np.asarray(vector, dtype=DTYPE).tobytes()


def from_blob(blob: bytes, dim: int) -> np.ndarray:
    """Deserialise one stored vector."""
    array = np.frombuffer(blob, dtype=DTYPE)
    if array.size != dim:
        raise VectorStoreError(
            f"Stored vector has {array.size} values but dim={dim}"
        )
    return array


def cosine_top_k(matrix: np.ndarray, query: np.ndarray, k: int) -> List[tuple]:
    """Exact top-k by cosine similarity over pre-normalised rows.

    Returns ``[(row_index, score), ...]`` sorted by descending score. Ties break
    on the lower row index so results are stable for identical inputs — the
    determinism guarantee this store exists to provide.
    """
    if matrix.size == 0:
        return []
    scores = matrix @ query  # rows are unit vectors -> dot product == cosine
    k = max(0, min(k, scores.shape[0]))
    if k == 0:
        return []
    # argsort on (-score, index) keeps ordering deterministic across ties.
    order = sorted(range(scores.shape[0]), key=lambda i: (-float(scores[i]), i))
    return [(i, float(scores[i])) for i in order[:k]]


# --------------------------------------------------------------------------- #
# SQLite-backed store
# --------------------------------------------------------------------------- #
def init_vector_schema(conn: sqlite3.Connection) -> None:
    """Create the embeddings table/indexes if absent."""
    conn.executescript(VECTOR_SCHEMA)
    conn.commit()


class SqliteVectorStore:
    """Exact-search vector store over the project's existing SQLite database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        #: ``(kind, sources) -> (rows, matrix)``. Analysis queries the same
        #: corpus once per page — twice with ``--use-priors`` — and each query
        #: re-read every row and rebuilt the whole matrix. The corpus cannot
        #: change under us between those queries: every write goes through
        #: ``add``/``delete`` on this instance, and both drop the cache.
        self._corpus: Dict[tuple, tuple] = {}
        init_vector_schema(conn)

    def _invalidate(self) -> None:
        """Drop the cached corpus. Called by every write path."""
        self._corpus.clear()

    # -- writes ------------------------------------------------------------ #
    def add(
        self,
        documents: Sequence[Document],
        vectors: Any,
        *,
        model: str,
        created_at: Optional[str] = None,
    ) -> int:
        """Upsert documents with their embeddings. Returns the number written.

        Re-adding an existing ``doc_id`` replaces it, so re-embedding a changed
        playbook updates in place instead of duplicating it in the corpus.
        """
        if not documents:
            return 0
        matrix = normalize(vectors)
        if matrix.shape[0] != len(documents):
            raise VectorStoreError(
                f"Got {len(documents)} documents but {matrix.shape[0]} vectors"
            )
        if not model:
            raise VectorStoreError("model is required — it records how a vector was produced")

        dim = int(matrix.shape[1])
        self._assert_dim_consistent(dim)

        rows = [
            (
                doc.doc_id, doc.kind, doc.text, doc.source,
                json.dumps(doc.metadata, sort_keys=True), model, dim,
                to_blob(matrix[i]), created_at,
            )
            for i, doc in enumerate(documents)
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO embeddings"
                " (doc_id, kind, text, source, metadata, model, dim, vector, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        self._invalidate()
        return len(rows)

    def _assert_dim_consistent(self, dim: int) -> None:
        """Reject mixing embedding dimensions in one corpus.

        Different dimensions mean different models; silently mixing them makes
        similarity scores meaningless rather than merely wrong.
        """
        row = self._conn.execute(
            "SELECT dim, model FROM embeddings LIMIT 1"
        ).fetchone()
        if row is not None and int(row["dim"]) != dim:
            raise VectorStoreError(
                f"Corpus stores {row['dim']}-dim vectors (model {row['model']!r}); "
                f"refusing to add {dim}-dim vectors. Re-embed the corpus to switch models."
            )

    def delete(
        self,
        *,
        doc_ids: Optional[Sequence[str]] = None,
        kind: Optional[str] = None,
    ) -> int:
        """Delete by explicit ids and/or whole ``kind``. Returns rows removed."""
        if doc_ids is None and kind is None:
            raise VectorStoreError("Refusing to delete everything: pass doc_ids or kind")
        clauses, params = [], []
        if doc_ids is not None:
            if not doc_ids:
                return 0
            clauses.append(f"doc_id IN ({', '.join('?' for _ in doc_ids)})")
            params.extend(doc_ids)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        with self._conn:
            cur = self._conn.execute(
                f"DELETE FROM embeddings WHERE {' AND '.join(clauses)}", params
            )
        self._invalidate()
        return cur.rowcount

    # -- reads ------------------------------------------------------------- #
    def count(self, *, kind: Optional[str] = None) -> int:
        if kind is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM embeddings WHERE kind = ?", (kind,)
            ).fetchone()
        return int(row["n"])

    def _load(self, kind: Optional[str], sources: Optional[Sequence[str]]):
        sql = "SELECT doc_id, kind, text, source, metadata, dim, vector FROM embeddings"
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if sources:
            clauses.append(f"source IN ({', '.join('?' for _ in sources)})")
            params.extend(sources)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # Stable ordering makes the tie-break in cosine_top_k reproducible.
        sql += " ORDER BY doc_id ASC"
        return self._conn.execute(sql, params).fetchall()

    def _corpus_matrix(self, kind, sources):
        """The rows and stacked matrix for one scope, built at most once.

        Keyed by the scope because ``kind="knowledge"`` and ``kind="finding"``
        are different corpora; a shared entry would answer a playbook query
        with prior findings.
        """
        key = (kind, tuple(sources) if sources else None)
        cached = self._corpus.get(key)
        if cached is not None:
            return cached

        rows = self._load(kind, sources)
        if not rows:
            # Not cached: an empty corpus is usually a corpus not yet embedded,
            # and the next call is the one that fills it.
            return rows, None
        dim = int(rows[0]["dim"])
        matrix = np.vstack([from_blob(r["vector"], dim) for r in rows])
        self._corpus[key] = (rows, matrix)
        return rows, matrix

    def query(
        self,
        vector: Any,
        *,
        k: int = 5,
        kind: Optional[str] = None,
        sources: Optional[Sequence[str]] = None,
        min_score: Optional[float] = None,
    ) -> List[SearchHit]:
        """Exact top-k cosine search, optionally scoped to a kind/sources.

        ``min_score`` drops weak matches so the LLM is not grounded in
        irrelevant context merely because the corpus was small.
        """
        if k <= 0:
            return []
        rows, matrix = self._corpus_matrix(kind, sources)
        if not rows:
            return []

        dim = int(rows[0]["dim"])
        query_vec = normalize(vector)[0]
        if query_vec.shape[0] != dim:
            raise VectorStoreError(
                f"Query vector has {query_vec.shape[0]} dims but corpus stores {dim}"
            )

        hits = []
        for index, score in cosine_top_k(matrix, query_vec, k):
            if min_score is not None and score < min_score:
                continue
            row = rows[index]
            hits.append(
                SearchHit(
                    doc_id=row["doc_id"],
                    text=row["text"],
                    kind=row["kind"],
                    source=row["source"],
                    metadata=json.loads(row["metadata"] or "{}"),
                    score=score,
                )
            )
        return hits

    def get(self, doc_id: str) -> Optional[Document]:
        """Fetch a stored document (without its vector)."""
        row = self._conn.execute(
            "SELECT doc_id, kind, text, source, metadata FROM embeddings WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        return Document(
            doc_id=row["doc_id"], text=row["text"], kind=row["kind"],
            source=row["source"], metadata=json.loads(row["metadata"] or "{}"),
        )
