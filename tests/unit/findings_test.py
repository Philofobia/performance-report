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
