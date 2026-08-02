"""Integration tests for the analysis pipeline and its CLI.

Everything is offline: runs come from temp JSON files, the LLM and embedding
clients are fakes, and the vector store is in-memory.
"""
from __future__ import annotations

import json

import pytest

from analysis.__main__ import (
    group_by_page,
    load_runs,
    main,
    persist_findings,
    rule_based_summary,
    run_analysis,
)
from analysis.llm import LlmPageAnalysis, LlmSummary
from analysis.reportmodel import Report, stable_payload
from normalize.schema import Run
from store import sql
from store.vectordb import SqliteVectorStore


def run_payload(run_id, page, device="mid-mobile", network="slow-4g", lcp=6200):
    return {
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com"},
        "page": {"name": page, "url": f"https://example.com/{page}"},
        "condition": {"device": device, "network": network,
                      "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": "2026-01-08T14:30:00Z", "source": "automated"},
        "metrics": {
            "cwp": {"lcp_ms": lcp, "cls": 0.42, "inp_ms": 480, "fcp_ms": 3100,
                    "ttfb_ms": 1800, "tbt_ms": 620},
            "network": {"total_transfer_kb": 4820, "request_count": 118,
                        "render_blocking_css": 6},
            "main_thread": {"script_ms": 1820, "task_ms": 3100, "dom_nodes": 3200},
        },
        "resource_timings": [
            {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140,
             "duration_ms": 390},
        ],
    }


@pytest.fixture
def input_dir(tmp_path):
    directory = tmp_path / "processed"
    directory.mkdir()
    for run_id, page in (("run_h1", "homepage"), ("run_h2", "homepage"),
                         ("run_p1", "plp")):
        device = "desktop" if run_id == "run_h2" else "mid-mobile"
        payload = run_payload(run_id, page, device=device)
        (directory / f"{run_id}.json").write_text(json.dumps(payload),
                                                  encoding="utf-8")
    return directory


class FakeEmbeddings:
    model = "fake-embed"

    def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeLlm:
    model = "fake-llm"

    def __init__(self):
        self.page_calls = 0
        self.summary_calls = 0

    def analyze_page(self, prompt):
        self.page_calls += 1
        source = next((s for s in prompt.sources if s.endswith(".md")), "images.md")
        return LlmPageAnalysis.model_validate({
            "summary": "Media weight dominates.",
            "findings": [{"title": "Hero media is heavy", "detail": "2140KB",
                          "evidence": ["lcp_ms=6200"],
                          "symptom_codes": ["lcp_fail"]}],
            "impacts": [{"audience": "ux", "text": "Empty hero."}],
            "recommendations": [{"title": "Compress the hero",
                                 "rationale": "Fewer bytes on the LCP path.",
                                 "playbook_source": source,
                                 "playbook_section": "Serve modern formats"}],
        })

    def summarize(self, payload):
        self.summary_calls += 1
        return LlmSummary(problem="Storefront is slow.",
                          key_finding="Media weight.",
                          top_actions=["Compress the hero"])


@pytest.fixture
def vector_store():
    from rag import knowledge

    conn = sql.connect(":memory:")
    store = SqliteVectorStore(conn)
    knowledge.index_knowledge(store, FakeEmbeddings(), directory="data/knowledge")
    return store


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def test_load_runs_reads_every_json_in_the_directory(input_dir):
    runs = load_runs(input_dir=input_dir)
    assert {r.run_id for r in runs} == {"run_h1", "run_h2", "run_p1"}


def test_load_runs_filters_by_page(input_dir):
    runs = load_runs(input_dir=input_dir, pages=["plp"])
    assert [r.run_id for r in runs] == ["run_p1"]


def test_load_runs_from_the_sqlite_store(tmp_path):
    db = tmp_path / "runs.sqlite"
    conn = sql.connect(db)
    sql.init_schema(conn)
    sql.insert_run(conn, Run.model_validate(run_payload("run_s1", "homepage")))
    conn.close()
    runs = load_runs(from_store=db)
    assert [r.run_id for r in runs] == ["run_s1"]


def test_load_runs_errors_when_nothing_is_found(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        load_runs(input_dir=empty)


def test_load_runs_reports_an_unreadable_file(tmp_path):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_runs(input_dir=directory)


def test_group_by_page_is_sorted(input_dir):
    grouped = group_by_page(load_runs(input_dir=input_dir))
    assert list(grouped) == ["homepage", "plp"]
    assert len(grouped["homepage"]) == 2


# --------------------------------------------------------------------------- #
# pipeline
# --------------------------------------------------------------------------- #
def test_run_analysis_produces_a_report_for_every_page(input_dir, vector_store):
    llm = FakeLlm()
    report = run_analysis(load_runs(input_dir=input_dir), store=vector_store,
                          embed_client=FakeEmbeddings(), llm_client=llm)
    assert isinstance(report, Report)
    assert [p.name for p in report.pages] == ["homepage", "plp"]
    assert report.meta.analysis_mode == "llm"


def test_one_call_per_page_plus_one_summary(input_dir, vector_store):
    llm = FakeLlm()
    run_analysis(load_runs(input_dir=input_dir), store=vector_store,
                 embed_client=FakeEmbeddings(), llm_client=llm)
    assert llm.page_calls == 2      # homepage, plp
    assert llm.summary_calls == 1


def test_pipeline_is_deterministic(input_dir, vector_store):
    runs = load_runs(input_dir=input_dir)
    first = run_analysis(runs, store=vector_store, embed_client=FakeEmbeddings(),
                         llm_client=FakeLlm())
    second = run_analysis(runs, store=vector_store, embed_client=FakeEmbeddings(),
                          llm_client=FakeLlm())
    assert stable_payload(first) == stable_payload(second)


def test_no_clients_degrades_to_rule_based(input_dir):
    report = run_analysis(load_runs(input_dir=input_dir), store=None,
                          embed_client=None, llm_client=None)
    assert report.meta.analysis_mode == "rule_based"
    assert report.meta.degradation_reason == "no_api_key"
    assert report.pages[0].recommendations
    assert report.summary.problem


def test_use_priors_retrieves_prior_findings(input_dir, vector_store):
    llm = FakeLlm()
    collected = []
    report = run_analysis(load_runs(input_dir=input_dir), store=vector_store,
                          embed_client=FakeEmbeddings(), llm_client=llm,
                          page_analyses_out=collected)
    persist_findings(vector_store, FakeEmbeddings(), report, collected)

    second = run_analysis(load_runs(input_dir=input_dir), store=vector_store,
                          embed_client=FakeEmbeddings(), llm_client=FakeLlm(),
                          use_priors=True)
    assert second.meta.analysis_mode == "llm"


def test_rule_based_summary_names_the_worst_page(input_dir):
    from analysis.findings import analyze_page
    from rag import knowledge, retrieve

    runs = load_runs(input_dir=input_dir)
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    pages = [
        analyze_page(group, hits=[], symptoms=retrieve.detect_symptoms(group[0]),
                     client=None, chunks=chunks)
        for group in group_by_page(runs).values()
    ]
    summary = rule_based_summary(pages)
    assert summary.problem
    assert 1 <= len(summary.top_actions) <= 3


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def test_findings_are_persisted_as_finding_documents(input_dir, vector_store):
    runs = load_runs(input_dir=input_dir)
    collected = []
    report = run_analysis(runs, store=vector_store, embed_client=FakeEmbeddings(),
                          llm_client=FakeLlm(), page_analyses_out=collected)
    written = persist_findings(vector_store, FakeEmbeddings(), report, collected)
    assert written == 2
    hits = vector_store.query([1.0, 0.0, 0.0, 0.0], k=5, kind="finding")
    assert {h.doc_id for h in hits} == {
        f"finding:{report.cover.campaign_id}:homepage",
        f"finding:{report.cover.campaign_id}:plp",
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_writes_a_report_json(input_dir, tmp_path, capsys):
    out = tmp_path / "reports"
    code = main(["--input-dir", str(input_dir), "--output-dir", str(out),
                 "--no-llm"])
    assert code == 0
    written = list(out.glob("*/report.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["meta"]["analysis_mode"] == "rule_based"
    # --no-llm is the user's choice, not a missing key
    assert payload["meta"]["degradation_reason"] == "llm_disabled"
    assert str(written[0]) in capsys.readouterr().out


def test_cli_filters_by_page(input_dir, tmp_path):
    out = tmp_path / "reports"
    assert main(["--input-dir", str(input_dir), "--output-dir", str(out),
                 "--pages", "plp", "--no-llm"]) == 0
    payload = json.loads(next(out.glob("*/report.json")).read_text(encoding="utf-8"))
    assert payload["cover"]["pages"] == ["plp"]


def test_cli_rejects_conflicting_sources(input_dir, tmp_path):
    assert main(["--input-dir", str(input_dir), "--from-store",
                 str(tmp_path / "x.sqlite")]) != 0


def test_cli_reports_missing_runs_without_a_traceback(tmp_path, capsys):
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert main(["--input-dir", str(empty), "--no-llm"]) != 0
    assert "No runs" in capsys.readouterr().err
