"""Unit tests for analysis/priority.py — one ordered plan across all pages."""
from __future__ import annotations

from analysis.priority import PLAN_LIMIT, rank_actions
from analysis.reportmodel import (
    PageBlock,
    ProjectionModel,
    RecommendationModel,
    SymptomModel,
)
from config.load import Thresholds
from report.glossary import load_glossary


def _page(name, *, metric, value, severity, gain, title, effort="medium"):
    """A page carrying exactly one recommendation with one projection."""
    return PageBlock(
        name=name, url=f"https://example.com/{name}", primary_run_id=f"run_{name}",
        verdict="fail", conditions=[], metrics={metric: value}, targets={},
        symptoms=[SymptomModel(code=f"{metric}_x", text="t", severity=severity,
                               metric=metric, value=value, target=None)],
        resources=[], resource_type_totals={},
        summary="s", findings=[], impacts=[], projections={},
        recommendations=[RecommendationModel(
            title=title, rationale="r", playbook_source="javascript.md",
            playbook_section="s", effort=effort, magnitude="medium",
            projections=[ProjectionModel(
                metric=metric, before=value, after_low=value - gain,
                after_high=value - gain, reduction_pct=10.0,
                source="javascript.md")],
        )],
    )


def _rank(pages):
    return rank_actions(pages, glossary=load_glossary(), thresholds=Thresholds())


def test_the_worse_page_outranks_the_better_one():
    """The live campaign ranked a 2041ms homepage action above an 8636ms PDP
    one, because pages were walked in alphabetical order."""
    pages = [
        _page("homepage", metric="tbt_ms", value=2041.0, severity="fail",
              gain=408.0, title="Break up long tasks"),
        _page("pdp", metric="tbt_ms", value=8636.0, severity="fail",
              gain=1727.0, title="Defer third-party scripts"),
    ]

    plan = _rank(pages)

    assert [a.page for a in plan] == ["pdp", "homepage"]
    assert [a.rank for a in plan] == [1, 2]


def test_severity_outweighs_a_marginally_larger_gain():
    pages = [
        _page("a", metric="tbt_ms", value=1000.0, severity="warn", gain=200.0,
              title="Warn action"),
        _page("b", metric="tbt_ms", value=1000.0, severity="fail", gain=150.0,
              title="Fail action"),
    ]

    assert _rank(pages)[0].page == "b"


def test_a_gain_is_capped_at_the_gap_to_target():
    """Improving a metric past its target earns no extra credit."""
    pages = [
        _page("a", metric="lcp_ms", value=2600.0, severity="fail", gain=2000.0,
              title="Huge claimed gain, small real gap"),
        _page("b", metric="lcp_ms", value=6000.0, severity="fail", gain=1000.0,
              title="Real gap"),
    ]

    assert _rank(pages)[0].page == "b"


def test_ties_break_on_page_then_title_so_runs_are_identical():
    pages = [
        _page("b", metric="tbt_ms", value=1000.0, severity="fail", gain=100.0,
              title="Zebra"),
        _page("a", metric="tbt_ms", value=1000.0, severity="fail", gain=100.0,
              title="Apple"),
    ]

    first = _rank(pages)
    second = _rank(list(reversed(pages)))

    assert [(a.page, a.title) for a in first] == [("a", "Apple"), ("b", "Zebra")]
    assert [(a.page, a.title) for a in first] == [(a.page, a.title) for a in second]


def test_projected_change_is_rendered_ready_to_print():
    pages = [_page("a", metric="tbt_ms", value=2041.0, severity="fail",
                   gain=408.0, title="Break up long tasks")]

    action = _rank(pages)[0]

    assert action.projected == "2041 ms → 1633 ms"
    assert action.metric == "tbt_ms"


def _page_with_band(name, *, value, conservative, optimistic, title):
    """One recommendation whose projection has a real low/high band."""
    page = _page(name, metric="lcp_ms", value=value, severity="fail",
                 gain=0.0, title=title)
    page.recommendations[0].projections = [ProjectionModel(
        metric="lcp_ms", before=value, after_low=conservative,
        after_high=optimistic, reduction_pct=(value - conservative) / value,
        source="images.md")]
    return page


def test_the_plan_prints_the_conservative_end_of_the_band():
    """`after_low` is the conservative end (analysis/estimator.py).

    The plan printed `after_high` — the optimistic edge — under a heading that
    promises "the conservative end of its stated range". On the live Oakley
    homepage that read 6524 ms → 2662 ms where the estimate was 4880 ms.
    """
    page = _page_with_band("a", value=6524.0, conservative=5546.0,
                           optimistic=3915.0, title="Always set width and height")

    assert _rank([page])[0].projected == "6524 ms → 5546 ms"


def test_the_plan_is_ordered_by_the_conservative_gain():
    """A wide optimistic band must not outrank a surer, larger conservative win."""
    wide = _page_with_band("wide", value=6000.0, conservative=5900.0,
                           optimistic=3000.0, title="Wide band")
    sure = _page_with_band("sure", value=6000.0, conservative=5000.0,
                           optimistic=4800.0, title="Sure thing")

    assert [a.page for a in _rank([wide, sure])] == ["sure", "wide"]


def test_a_recommendation_without_projections_still_makes_the_plan():
    """Rule-based campaigns have no magnitudes; they still have actions."""
    page = PageBlock(
        name="a", url="https://example.com/a", primary_run_id="run_a",
        verdict="fail", conditions=[], metrics={}, targets={}, symptoms=[],
        resources=[], resource_type_totals={}, summary="s", findings=[],
        impacts=[], projections={},
        recommendations=[RecommendationModel(
            title="Do the thing", rationale="r", playbook_source="caching.md",
            playbook_section="s", effort="low", magnitude="unknown",
            projections=[])],
    )

    plan = _rank([page])

    assert [a.title for a in plan] == ["Do the thing"]
    assert plan[0].projected == ""
    assert plan[0].metric is None


def test_no_recommendations_is_an_empty_plan_not_a_crash():
    page = PageBlock(
        name="a", url="https://example.com/a", primary_run_id="run_a",
        verdict="pass", conditions=[], metrics={}, targets={}, symptoms=[],
        resources=[], resource_type_totals={}, summary="s", findings=[],
        impacts=[], projections={}, recommendations=[],
    )

    assert _rank([page]) == []


def test_the_plan_carries_the_models_plain_rationale():
    page = _page("a", metric="tbt_ms", value=1000.0, severity="fail", gain=100.0,
                 title="Break up long tasks")
    page.recommendations[0].why_it_matters = "The page reacts to taps sooner."

    assert _rank([page])[0].why_it_matters == "The page reacts to taps sooner."


def test_a_page_target_beats_the_glossary_default():
    """PageBlock.targets is what that campaign was judged against."""
    page = _page("a", metric="lcp_ms", value=6000.0, severity="fail",
                 gain=5000.0, title="Big fix")
    page.targets = {"lcp_ms": 5900.0}

    # Gain is capped at the 100ms gap, not the claimed 5000ms.
    assert _rank([page])[0].projected == "6000 ms → 1000 ms"


def test_the_plan_is_capped_so_it_reads_as_a_plan():
    """Eighteen actions is a backlog. The rest still appear under their pages."""
    pages = [
        _page(f"p{i}", metric="tbt_ms", value=1000.0 + i, severity="fail",
              gain=100.0 + i, title=f"Action {i}")
        for i in range(12)
    ]

    plan = _rank(pages)

    assert len(plan) == PLAN_LIMIT
    assert [a.rank for a in plan] == list(range(1, PLAN_LIMIT + 1))


def test_the_cap_keeps_the_highest_scoring_actions():
    pages = [
        _page("small", metric="tbt_ms", value=1000.0, severity="fail", gain=10.0,
              title="Small"),
    ] + [
        _page(f"big{i}", metric="tbt_ms", value=9000.0, severity="fail",
              gain=2000.0 + i, title=f"Big {i}")
        for i in range(PLAN_LIMIT)
    ]

    titles = [a.title for a in _rank(pages)]

    assert "Small" not in titles
