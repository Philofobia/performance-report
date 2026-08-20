"""Integration tests for store/vectordb.py (exact-search vector store).

In-memory SQLite, deterministic hand-built vectors — no embedding API is
called, so these run offline (TESTING_PLAN.md §3).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from store import sql
from store.vectordb import (
    Document,
    SqliteVectorStore,
    VectorStoreError,
    cosine_top_k,
    from_blob,
    normalize,
    to_blob,
)

MODEL = "text-embedding-004"


@pytest.fixture
def store():
    conn = sql.connect(":memory:")
    yield SqliteVectorStore(conn)
    conn.close()


def docs(*ids, kind="knowledge", source=None):
    return [
        Document(doc_id=i, text=f"text for {i}", kind=kind, source=source,
                 metadata={"n": n})
        for n, i in enumerate(ids)
    ]


# --------------------------------------------------------------------------- #
# pure vector helpers
# --------------------------------------------------------------------------- #
def test_normalize_produces_unit_rows():
    out = normalize([[3.0, 4.0], [1.0, 0.0]])
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)
    assert np.allclose(out[0], [0.6, 0.8])


def test_normalize_accepts_a_single_vector():
    out = normalize([3.0, 4.0])
    assert out.shape == (1, 2)
    assert np.allclose(out[0], [0.6, 0.8])


def test_normalize_leaves_zero_vector_as_zeros_not_nan():
    """A zero vector means 'no signal' — it must not poison the corpus."""
    out = normalize([[0.0, 0.0]])
    assert not np.isnan(out).any()
    assert np.allclose(out[0], [0.0, 0.0])


def test_normalize_rejects_3d_input():
    with pytest.raises(VectorStoreError, match="1-D or 2-D"):
        normalize(np.zeros((2, 2, 2)))


def test_blob_round_trip_preserves_values():
    vector = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    assert np.allclose(from_blob(to_blob(vector), 3), vector)


def test_from_blob_detects_dimension_mismatch():
    with pytest.raises(VectorStoreError, match="dim="):
        from_blob(to_blob(np.zeros(4, dtype=np.float32)), 3)


def test_cosine_top_k_orders_by_similarity():
    matrix = normalize([[1, 0], [0.9, 0.1], [0, 1]])
    query = normalize([1, 0])[0]
    result = cosine_top_k(matrix, query, k=3)
    assert [i for i, _ in result] == [0, 1, 2]
    assert math.isclose(result[0][1], 1.0, rel_tol=1e-6)


def test_cosine_top_k_handles_empty_and_oversized_k():
    assert cosine_top_k(np.zeros((0, 3)), np.zeros(3), k=5) == []
    matrix = normalize([[1, 0], [0, 1]])
    assert len(cosine_top_k(matrix, normalize([1, 0])[0], k=99)) == 2
    assert cosine_top_k(matrix, normalize([1, 0])[0], k=0) == []


def test_cosine_top_k_breaks_ties_deterministically():
    """Identical scores must resolve identically every call — §6.2."""
    matrix = normalize([[1, 0], [1, 0], [1, 0]])
    query = normalize([1, 0])[0]
    for _ in range(5):
        assert [i for i, _ in cosine_top_k(matrix, query, k=2)] == [0, 1]


# --------------------------------------------------------------------------- #
# add / count / get
# --------------------------------------------------------------------------- #
def test_add_and_count(store):
    assert store.add(docs("a", "b"), [[1, 0], [0, 1]], model=MODEL) == 2
    assert store.count() == 2


def test_add_empty_is_a_noop(store):
    assert store.add([], [], model=MODEL) == 0
    assert store.count() == 0


def test_add_rejects_mismatched_document_and_vector_counts(store):
    with pytest.raises(VectorStoreError, match="2 documents but 1 vectors"):
        store.add(docs("a", "b"), [[1, 0]], model=MODEL)


def test_add_requires_a_model_name(store):
    """The model is how we know a vector is comparable to the others."""
    with pytest.raises(VectorStoreError, match="model is required"):
        store.add(docs("a"), [[1, 0]], model="")


def test_re_adding_a_doc_id_replaces_it(store):
    store.add(docs("a"), [[1, 0]], model=MODEL)
    store.add([Document(doc_id="a", text="updated playbook")], [[0, 1]], model=MODEL)
    assert store.count() == 1
    assert store.get("a").text == "updated playbook"


def test_mixing_embedding_dimensions_is_refused(store):
    """Different dims mean different models; mixing makes scores meaningless."""
    store.add(docs("a"), [[1, 0]], model=MODEL)
    with pytest.raises(VectorStoreError, match="refusing to add"):
        store.add(docs("b"), [[1, 0, 0]], model="other-model")


def test_get_returns_none_for_unknown_doc(store):
    assert store.get("nope") is None


def test_get_round_trips_metadata(store):
    store.add(
        [Document(doc_id="a", text="t", kind="finding", source="run_1",
                  metadata={"page": "homepage", "lcp": 6200})],
        [[1, 0]], model=MODEL,
    )
    doc = store.get("a")
    assert doc.metadata == {"page": "homepage", "lcp": 6200}
    assert doc.kind == "finding" and doc.source == "run_1"


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #
@pytest.fixture
def corpus(store):
    store.add(
        [
            Document("images", "compress and lazy-load images", "knowledge", "images.md"),
            Document("fonts", "preload fonts and use font-display", "knowledge", "fonts.md"),
            Document("split", "code splitting reduces bundle size", "knowledge", "split.md"),
            Document("finding1", "homepage hero video is 2MB", "finding", "run_1"),
        ],
        [[1, 0, 0], [0, 1, 0], [0, 0, 1], [0.9, 0.1, 0]],
        model=MODEL,
    )
    return store


def test_query_returns_nearest_first(corpus):
    hits = corpus.query([1, 0, 0], k=2)
    assert [h.doc_id for h in hits] == ["images", "finding1"]
    assert hits[0].score > hits[1].score


def test_query_scores_are_cosine_similarities(corpus):
    hits = corpus.query([1, 0, 0], k=1)
    assert math.isclose(hits[0].score, 1.0, rel_tol=1e-6)


def test_query_returns_full_hit_payload(corpus):
    hit = corpus.query([0, 1, 0], k=1)[0]
    assert hit.doc_id == "fonts"
    assert hit.text == "preload fonts and use font-display"
    assert hit.kind == "knowledge"
    assert hit.source == "fonts.md"


def test_query_can_scope_to_a_kind(corpus):
    """Playbooks and prior findings are retrieved as separate corpora."""
    hits = corpus.query([1, 0, 0], k=5, kind="finding")
    assert [h.doc_id for h in hits] == ["finding1"]


def test_query_can_scope_to_sources(corpus):
    hits = corpus.query([1, 0, 0], k=5, sources=["fonts.md", "split.md"])
    assert {h.doc_id for h in hits} == {"fonts", "split"}


def test_query_min_score_filters_weak_matches(corpus):
    """A small corpus always returns *something*; min_score keeps it relevant."""
    unfiltered = corpus.query([0, 1, 0], k=4)
    assert len(unfiltered) == 4  # every doc comes back, however irrelevant

    filtered = corpus.query([0, 1, 0], k=4, min_score=0.99)
    assert [h.doc_id for h in filtered] == ["fonts"]  # only the true match

    # And the threshold is applied to the score, not to rank.
    assert all(h.score >= 0.5 for h in corpus.query([0, 0, 1], k=4, min_score=0.5))


def test_query_on_empty_store_returns_empty(store):
    assert store.query([1, 0, 0], k=5) == []


def test_query_with_non_positive_k_returns_empty(corpus):
    assert corpus.query([1, 0, 0], k=0) == []


def test_query_rejects_wrong_dimension(corpus):
    with pytest.raises(VectorStoreError, match="dims but corpus stores"):
        corpus.query([1, 0], k=1)


def test_query_is_deterministic_across_repeated_calls(corpus):
    """§6.2: identical inputs must retrieve identical context, every time."""
    first = [(h.doc_id, h.score) for h in corpus.query([0.5, 0.5, 0], k=4)]
    for _ in range(5):
        assert [(h.doc_id, h.score) for h in corpus.query([0.5, 0.5, 0], k=4)] == first


def test_results_are_exact_not_approximate(corpus):
    """Brute force must return the true nearest neighbour, never an approximation."""
    query = [0.8, 0.6, 0.0]
    hits = corpus.query(query, k=4)
    stored = {
        "images": [1, 0, 0], "fonts": [0, 1, 0],
        "split": [0, 0, 1], "finding1": [0.9, 0.1, 0],
    }
    q = normalize(query)[0]
    expected = sorted(
        stored, key=lambda d: -float(normalize(stored[d])[0] @ q)
    )
    assert [h.doc_id for h in hits] == expected


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
def test_delete_by_doc_ids(corpus):
    assert corpus.delete(doc_ids=["images", "fonts"]) == 2
    assert corpus.count() == 2


def test_delete_by_kind(corpus):
    assert corpus.delete(kind="knowledge") == 3
    assert corpus.count() == 1


def test_delete_requires_a_filter(corpus):
    """Guard against wiping the corpus with an empty call."""
    with pytest.raises(VectorStoreError, match="Refusing to delete everything"):
        corpus.delete()
    assert corpus.count() == 4


def test_delete_with_empty_id_list_is_a_noop(corpus):
    assert corpus.delete(doc_ids=[]) == 0
    assert corpus.count() == 4


# --------------------------------------------------------------------------- #
# coexistence with the runs store
# --------------------------------------------------------------------------- #
def test_embeddings_share_the_runs_database(tmp_path):
    """One file, one backup — a run and its embedding commit together."""
    path = tmp_path / "runs.sqlite"
    conn = sql.connect(path)
    store = SqliteVectorStore(conn)
    store.add(docs("a"), [[1, 0]], model=MODEL)
    conn.close()

    reopened = sql.connect(path)
    assert SqliteVectorStore(reopened).count() == 1
    tables = {r["name"] for r in reopened.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "embeddings"} <= tables
    reopened.close()


def test_vector_schema_init_is_idempotent(store):
    store.add(docs("a"), [[1, 0]], model=MODEL)
    SqliteVectorStore(store._conn)  # re-init must not wipe existing rows
    assert store.count() == 1


# --------------------------------------------------------------------------- #
# corpus caching — the same corpus is re-read once per analysed page
# --------------------------------------------------------------------------- #
def corpus_reads(conn):
    """Count SELECTs against `embeddings` while the block runs."""
    statements = []
    conn.set_trace_callback(statements.append)
    return statements


def test_repeated_queries_read_the_corpus_once(store):
    """A 10-page campaign rebuilt the whole matrix 10 times (20 with priors)."""
    store.add(docs("a", "b", "c"), [[1, 0], [0, 1], [1, 1]], model=MODEL)
    statements = corpus_reads(store._conn)

    store.query([1, 0], k=2)
    store.query([0, 1], k=2)
    store.query([1, 1], k=2)

    loads = [s for s in statements if "FROM embeddings" in s and "SELECT doc_id" in s]
    assert len(loads) == 1


def test_queries_scoped_differently_do_not_share_a_cache(store):
    store.add(docs("a", kind="knowledge"), [[1, 0]], model=MODEL)
    store.add(docs("f", kind="finding"), [[0, 1]], model=MODEL)

    assert [h.doc_id for h in store.query([1, 0], k=5, kind="knowledge")] == ["a"]
    assert [h.doc_id for h in store.query([0, 1], k=5, kind="finding")] == ["f"]


def test_a_document_added_after_a_query_is_still_found(store):
    """Cache invalidation on write — the way a cache like this goes wrong."""
    store.add(docs("a"), [[1, 0]], model=MODEL)
    store.query([1, 0], k=5)

    store.add(docs("b"), [[0, 1]], model=MODEL)
    assert {h.doc_id for h in store.query([0, 1], k=5)} == {"a", "b"}


def test_a_deleted_document_stops_being_returned(store):
    store.add(docs("a", "b"), [[1, 0], [0, 1]], model=MODEL)
    store.query([1, 0], k=5)

    store.delete(doc_ids=["b"])
    assert [h.doc_id for h in store.query([0, 1], k=5)] == ["a"]


def test_a_replaced_document_returns_its_new_text(store):
    store.add(docs("a"), [[1, 0]], model=MODEL)
    store.query([1, 0], k=5)

    store.add([Document(doc_id="a", text="rewritten playbook")], [[1, 0]], model=MODEL)
    assert store.query([1, 0], k=5)[0].text == "rewritten playbook"
