"""Unit tests for analysis/findings.py — selection and the rule-based path.

No store, no client, no key: the rule-based path must work with nothing but
the runs and the playbooks on disk.
"""
from __future__ import annotations

import pytest

from analysis.findings import (
    Finding,
    Impact,
    Recommendation,
    match_playbooks_by_symptoms,
    rule_based_analysis,
    select_primary,
)
from config.load import Thresholds
from normalize.schema import Run
from rag import knowledge, retrieve


def make_run(run_id="run_a", lcp=6200, cls=0.42, inp=480, page="homepage",
             device="mid-mobile", network="slow-4g"):
    return Run.model_validate({
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com"},
        "page": {"name": page, "url": "https://example.com/"},
        "condition": {"device": device, "network": network,
                      "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": "2026-01-08T14:30:00Z", "source": "automated"},
        "metrics": {
            "cwp": {"lcp_ms": lcp, "cls": cls, "inp_ms": inp, "fcp_ms": 3100,
                    "ttfb_ms": 1800, "tbt_ms": 620},
            "network": {"total_transfer_kb": 4820, "request_count": 118,
                        "render_blocking_css": 6},
            "main_thread": {"script_ms": 1820, "task_ms": 3100, "dom_nodes": 3200},
        },
        "resource_timings": [
            {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140,
             "duration_ms": 390},
        ],
    })


def _symptoms_of(chunk):
    raw = chunk.metadata.get("symptoms", [])
    return raw if isinstance(raw, list) else [raw]


# --------------------------------------------------------------------------- #
# select_primary
# --------------------------------------------------------------------------- #
def test_primary_is_the_run_with_most_failing_symptoms():
    healthy = make_run("run_ok", lcp=1800, cls=0.02, inp=90)
    broken = make_run("run_bad", lcp=6200, cls=0.42, inp=480)
    assert select_primary([healthy, broken]).run_id == "run_bad"


def test_primary_breaks_ties_on_lcp_then_run_id():
    a = make_run("run_b", lcp=5000, cls=0.42, inp=480)
    b = make_run("run_a", lcp=5000, cls=0.42, inp=480)
    assert select_primary([a, b]).run_id == "run_a"


def test_primary_selection_is_order_independent():
    a = make_run("run_a", lcp=3000, cls=0.05, inp=100)
    b = make_run("run_b", lcp=6200, cls=0.42, inp=480)
    assert select_primary([a, b]).run_id == select_primary([b, a]).run_id


def test_primary_of_empty_raises():
    with pytest.raises(ValueError):
        select_primary([])


# --------------------------------------------------------------------------- #
# playbook matching
# --------------------------------------------------------------------------- #
def test_matching_selects_playbooks_whose_front_matter_lists_the_symptom():
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    matched = match_playbooks_by_symptoms(["lcp_fail"], chunks)
    assert matched
    assert all("lcp_fail" in _symptoms_of(c) for c in matched)


def test_matching_returns_nothing_for_an_unknown_symptom():
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    assert match_playbooks_by_symptoms(["no_such_symptom"], chunks) == []


def test_matching_is_deterministic_and_ordered_by_source_then_chunk():
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    first = match_playbooks_by_symptoms(["lcp_fail", "page_weight"], chunks)
    second = match_playbooks_by_symptoms(["page_weight", "lcp_fail"], chunks)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


# --------------------------------------------------------------------------- #
# rule_based_analysis
# --------------------------------------------------------------------------- #
def test_rule_based_analysis_produces_findings_from_symptoms():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    summary, findings, impacts, candidates = rule_based_analysis(run, symptoms, chunks)

    assert summary
    assert findings and all(isinstance(f, Finding) for f in findings)
    detected = {s.code for s in symptoms}
    for finding in findings:
        assert set(finding.symptom_codes) <= detected


def test_rule_based_analysis_produces_impacts_for_each_audience():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    _, _, impacts, _ = rule_based_analysis(run, symptoms, chunks)
    assert {i.audience for i in impacts} == {"ux", "seo", "business"}
    assert all(isinstance(i, Impact) for i in impacts)


def test_rule_based_recommendations_cite_a_real_playbook_section():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    _, _, _, recommendations = rule_based_analysis(run, symptoms, chunks)

    assert recommendations
    assert all(isinstance(r, Recommendation) for r in recommendations)
    sources = {c.source for c in chunks}
    for rec in recommendations:
        assert rec.playbook_source in sources
        assert rec.playbook_section
        assert rec.effort in {"low", "medium", "high", "unknown"}


def test_rule_based_recommendations_are_capped_and_deterministic():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    first = rule_based_analysis(run, symptoms, chunks)[3]
    second = rule_based_analysis(run, symptoms, chunks)[3]
    assert len(first) <= 6
    assert [r.title for r in first] == [r.title for r in second]


def test_rule_based_analysis_on_a_healthy_run_says_so():
    run = make_run(lcp=1500, cls=0.01, inp=80)
    run.metrics.cwp.fcp_ms = 1200
    run.metrics.cwp.ttfb_ms = 400
    run.metrics.cwp.tbt_ms = 50
    run.metrics.network.total_transfer_kb = 800
    run.metrics.network.request_count = 30
    run.metrics.network.render_blocking_css = 0
    run.metrics.main_thread.script_ms = 100
    run.resource_timings = []
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    summary, findings, impacts, recommendations = rule_based_analysis(
        run, symptoms, chunks
    )
    assert symptoms == []
    assert "no threshold" in summary.lower()
    assert findings == []
    assert recommendations == []


# --------------------------------------------------------------------------- #
# analyze_page — the LLM path and its degradations
# --------------------------------------------------------------------------- #
from analysis.findings import PageAnalysis, analyze_page  # noqa: E402
from analysis.llm import InvalidModelOutputError, LlmPageAnalysis  # noqa: E402
from rag.embeddings import QuotaExceededError  # noqa: E402
from store.vectordb import SearchHit  # noqa: E402


def a_hit(source="images.md", **meta):
    metadata = {"category": "images", "effort": "low",
                "expected_lcp_reduction_pct": [15, 40], "heading_path": ["Images"]}
    metadata.update(meta)
    return SearchHit(doc_id=f"{source}#x", text="Serve modern formats. Use AVIF.",
                     kind="knowledge", source=source, metadata=metadata, score=0.9)


class FakeClient:
    """Stands in for GoogleAnalysisClient."""

    model = "fake-llm"

    def __init__(self, page_result=None, error=None):
        self._page_result = page_result
        self._error = error
        self.calls = 0

    def analyze_page(self, prompt):
        self.calls += 1
        if self._error:
            raise self._error
        return self._page_result


def an_llm_result(source="images.md"):
    return LlmPageAnalysis.model_validate({
        "summary": "The hero video dominates the LCP path.",
        "findings": [{"title": "Hero video is the LCP element",
                      "detail": "2140KB before first paint.",
                      "evidence": ["lcp_ms=6200"],
                      "symptom_codes": ["lcp_fail", "invented_code"]}],
        "impacts": [{"audience": "ux", "text": "Empty hero for six seconds."}],
        "recommendations": [{"title": "Replace the video with a poster",
                             "rationale": "Removes 2MB from the critical path.",
                             "playbook_source": source,
                             "playbook_section": "Serve modern formats"}],
    })


def _setup():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    return run, symptoms, chunks


def test_llm_path_produces_an_llm_mode_analysis():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[a_hit()], symptoms=symptoms,
                          client=FakeClient(an_llm_result()), chunks=chunks)
    assert isinstance(result, PageAnalysis)
    assert result.mode == "llm"
    assert result.degradation_reason is None
    assert result.summary.startswith("The hero video")
    assert result.playbooks_cited == ["images.md"]


def test_numbers_come_from_the_estimator_not_the_model():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[a_hit()], symptoms=symptoms,
                          client=FakeClient(an_llm_result()), chunks=chunks)
    rec = result.recommendations[0]
    assert rec.projections
    projection = rec.projections[0]
    assert projection.metric == "lcp_ms"
    assert projection.before == 6200.0
    assert projection.after_low == pytest.approx(6200 * 0.85)


def test_unknown_symptom_codes_are_pruned_but_the_finding_survives():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[a_hit()], symptoms=symptoms,
                          client=FakeClient(an_llm_result()), chunks=chunks)
    codes = result.findings[0].symptom_codes
    assert "lcp_fail" in codes
    assert "invented_code" not in codes


def test_a_recommendation_citing_an_unretrieved_playbook_is_dropped():
    run, symptoms, chunks = _setup()
    result = analyze_page(
        [run], hits=[a_hit("images.md")], symptoms=symptoms,
        client=FakeClient(an_llm_result(source="fabricated.md")), chunks=chunks
    )
    # every recommendation was dropped -> fell back for this page
    assert result.dropped_recommendations == 1
    assert result.mode == "rule_based"
    assert result.degradation_reason == "no_grounded_recommendations"


def test_partial_drop_keeps_the_grounded_recommendations():
    run, symptoms, chunks = _setup()
    mixed = LlmPageAnalysis.model_validate({
        "summary": "s",
        "findings": [],
        "impacts": [],
        "recommendations": [
            {"title": "Real", "rationale": "r", "playbook_source": "images.md",
             "playbook_section": "Serve modern formats"},
            {"title": "Fake", "rationale": "r", "playbook_source": "invented.md",
             "playbook_section": "Nope"},
        ],
    })
    result = analyze_page([run], hits=[a_hit("images.md")], symptoms=symptoms,
                          client=FakeClient(mixed), chunks=chunks)
    assert result.mode == "llm"
    assert result.dropped_recommendations == 1
    assert [r.title for r in result.recommendations] == ["Real"]


def test_no_client_falls_back_with_a_reason():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[], symptoms=symptoms, client=None,
                          chunks=chunks)
    assert result.mode == "rule_based"
    assert result.degradation_reason == "no_api_key"
    assert result.recommendations


def test_no_client_by_user_choice_is_not_reported_as_a_missing_key():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[], symptoms=symptoms, client=None,
                          chunks=chunks, no_client_reason="llm_disabled")
    assert result.mode == "rule_based"
    assert result.degradation_reason == "llm_disabled"


def test_invalid_model_output_falls_back_with_a_reason():
    run, symptoms, chunks = _setup()
    result = analyze_page(
        [run], hits=[a_hit()], symptoms=symptoms,
        client=FakeClient(error=InvalidModelOutputError("bad json")), chunks=chunks
    )
    assert result.mode == "rule_based"
    assert result.degradation_reason == "invalid_model_output"


def test_quota_exhaustion_falls_back_with_a_reason():
    run, symptoms, chunks = _setup()
    result = analyze_page(
        [run], hits=[a_hit()], symptoms=symptoms,
        client=FakeClient(error=QuotaExceededError("out")), chunks=chunks
    )
    assert result.mode == "rule_based"
    assert result.degradation_reason == "quota_exhausted"


def test_all_runs_for_the_page_are_retained_for_comparison():
    mobile = make_run("run_m", device="mid-mobile", network="slow-4g")
    desktop = make_run("run_d", lcp=2100, cls=0.02, inp=90,
                       device="desktop", network="fast-3g")
    _, symptoms, chunks = _setup()
    result = analyze_page([desktop, mobile], hits=[a_hit()], symptoms=symptoms,
                          client=FakeClient(an_llm_result()), chunks=chunks)
    assert result.primary_run.run_id == "run_m"
    assert [r.run_id for r in result.runs] == ["run_d", "run_m"]


def test_budget_exhaustion_falls_back_with_its_own_reason():
    """"We chose not to spend" must not be reported as a bad model response."""
    from rag.budget import BudgetExhaustedError

    run, symptoms, chunks = _setup()
    result = analyze_page(
        [run], hits=[a_hit()], symptoms=symptoms,
        client=FakeClient(error=BudgetExhaustedError("spent")), chunks=chunks
    )
    assert result.mode == "rule_based"
    assert result.degradation_reason == "budget_exhausted"
    assert result.recommendations


def test_an_unavailable_model_degrades_with_its_own_reason():
    """"The model is gone" is not "the model answered badly"."""
    from analysis.llm import LlmUnavailableError

    run, symptoms, chunks = _setup()
    result = analyze_page(
        [run], hits=[a_hit()], symptoms=symptoms,
        client=FakeClient(error=LlmUnavailableError("404 NOT_FOUND")), chunks=chunks
    )
    assert result.mode == "rule_based"
    assert result.degradation_reason == "model_unavailable"
    assert result.recommendations
