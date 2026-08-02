"""Unit tests for analysis/estimator.py — the rule-based projection math.

The estimator is pure: no store, no client, no config. Every number in the
report comes from here, so these tests are the guard against SPEC §11's
"LLM hallucinated improvement magnitudes" risk.
"""
from __future__ import annotations

import pytest

from analysis.estimator import (
    Candidate,
    ImpactRange,
    effort_of,
    parse_impact_ranges,
)


def test_parses_percentage_range_into_fractions():
    ranges = parse_impact_ranges({"expected_lcp_reduction_pct": [15, 40]})
    assert ranges == [ImpactRange(metric="lcp_ms", low=0.15, high=0.40, absolute=False)]


def test_parses_absolute_range_for_cls():
    ranges = parse_impact_ranges({"expected_cls_reduction_abs": [0.05, 0.15]})
    assert ranges == [ImpactRange(metric="cls", low=0.05, high=0.15, absolute=True)]


def test_single_scalar_becomes_a_degenerate_range():
    ranges = parse_impact_ranges({"expected_ttfb_reduction_pct": 30})
    assert ranges == [ImpactRange(metric="ttfb_ms", low=0.30, high=0.30, absolute=False)]


def test_ranges_are_sorted_by_metric_for_determinism():
    ranges = parse_impact_ranges({
        "expected_ttfb_reduction_pct": [30, 80],
        "expected_lcp_reduction_pct": [15, 40],
    })
    assert [r.metric for r in ranges] == ["lcp_ms", "ttfb_ms"]


def test_unknown_and_malformed_keys_are_ignored():
    meta = {
        "expected_bogus_reduction_pct": [10, 20],   # unknown metric stem
        "expected_lcp_reduction_pct": "not a number",
        "category": "images",
    }
    assert parse_impact_ranges(meta) == []


def test_reversed_bounds_are_normalised():
    ranges = parse_impact_ranges({"expected_lcp_reduction_pct": [40, 15]})
    assert ranges == [ImpactRange(metric="lcp_ms", low=0.15, high=0.40, absolute=False)]


def test_effort_defaults_to_unknown():
    assert effort_of({"effort": "low"}) == "low"
    assert effort_of({"effort": "LOW"}) == "low"
    assert effort_of({}) == "unknown"
    assert effort_of({"effort": "wildly speculative"}) == "unknown"


def test_candidate_is_hashable_and_carries_its_source():
    c = Candidate(source="images.md", metadata={"effort": "low"})
    assert c.source == "images.md"


# --------------------------------------------------------------------------- #
# Stacking, decay and aggregation
# --------------------------------------------------------------------------- #
from analysis.estimator import (  # noqa: E402 - grouped with the code it tests
    DECAY,
    MAX_TOTAL_REDUCTION,
    Projection,
    aggregate,
    by_source,
    project,
    rank_key,
)

METRICS = {"lcp_ms": 6200.0, "cls": 0.42, "ttfb_ms": 1800.0, "inp_ms": None}


def _cand(source, **meta):
    return Candidate(source=source, metadata=meta)


def test_single_candidate_uses_the_low_bound_for_the_headline():
    out = project([_cand("images.md", expected_lcp_reduction_pct=[15, 40])], METRICS)
    assert len(out) == 1
    p = out[0]
    assert p.metric == "lcp_ms"
    assert p.before == 6200.0
    assert p.after_low == pytest.approx(6200 * 0.85)     # conservative
    assert p.after_high == pytest.approx(6200 * 0.60)    # optimistic band edge
    assert p.reduction_pct == pytest.approx(0.15)
    assert p.source == "images.md"


def test_second_fix_on_the_same_metric_is_decayed():
    out = project(
        [
            _cand("images.md", expected_lcp_reduction_pct=[20, 40]),
            _cand("fonts.md", expected_lcp_reduction_pct=[10, 30]),
        ],
        METRICS,
    )
    first, second = out[0], out[1]
    assert first.source == "images.md"          # larger low bound applies first
    assert first.after_low == pytest.approx(6200 * 0.80)
    # second candidate's 10% is discounted by DECAY and applied to the remainder
    expected = first.after_low * (1 - 0.10 * DECAY)
    assert second.before == pytest.approx(first.after_low)
    assert second.after_low == pytest.approx(expected)


def test_cumulative_reduction_is_capped():
    heavy = [
        _cand(f"p{i}.md", expected_lcp_reduction_pct=[50, 60]) for i in range(6)
    ]
    out = project(heavy, METRICS)
    floor = 6200.0 * (1 - MAX_TOTAL_REDUCTION)
    assert min(p.after_low for p in out) >= floor - 1e-6
    assert out[-1].after_low == pytest.approx(floor)


def test_absolute_ranges_subtract_instead_of_scaling():
    out = project([_cand("fonts.md", expected_cls_reduction_abs=[0.05, 0.15])], METRICS)
    p = out[0]
    assert p.metric == "cls"
    assert p.after_low == pytest.approx(0.42 - 0.05)
    assert p.after_high == pytest.approx(0.42 - 0.15)


def test_an_oversized_absolute_delta_is_held_at_the_floor():
    # A 9.0 CLS reduction is nonsense against a 0.42 measurement. The 70% cap
    # catches it before the zero clamp ever has to.
    out = project([_cand("x.md", expected_cls_reduction_abs=[9.0, 9.0])], {"cls": 0.42})
    assert out[0].after_low == pytest.approx(0.42 * (1 - MAX_TOTAL_REDUCTION))


def test_projection_never_goes_negative_when_the_floor_is_zero():
    out = project([_cand("x.md", expected_cls_reduction_abs=[9.0, 9.0])], {"cls": 0.0})
    assert out[0].after_low == 0.0
    assert out[0].after_high == 0.0


def test_candidate_with_no_range_yields_nothing():
    assert project([_cand("prose-only.md", effort="low")], METRICS) == []


def test_range_for_an_unmeasured_metric_is_skipped():
    # inp_ms is None in METRICS - nothing to project from.
    assert project([_cand("js.md", expected_inp_reduction_pct=[20, 40])], METRICS) == []


def test_ordering_is_stable_for_equal_bounds():
    out = project(
        [
            _cand("zebra.md", expected_lcp_reduction_pct=[20, 40]),
            _cand("alpha.md", expected_lcp_reduction_pct=[20, 40]),
        ],
        METRICS,
    )
    assert [p.source for p in out] == ["alpha.md", "zebra.md"]


def test_aggregate_reports_first_before_and_last_after_per_metric():
    out = project(
        [
            _cand("images.md", expected_lcp_reduction_pct=[20, 40]),
            _cand("fonts.md", expected_lcp_reduction_pct=[10, 30]),
            _cand("caching.md", expected_ttfb_reduction_pct=[30, 80]),
        ],
        METRICS,
    )
    agg = aggregate(out, METRICS)
    assert set(agg) == {"lcp_ms", "ttfb_ms"}
    assert agg["lcp_ms"].before == 6200.0
    assert agg["lcp_ms"].after_low == pytest.approx(out[1].after_low)
    assert agg["lcp_ms"].source == "aggregate"
    assert agg["ttfb_ms"].reduction_pct == pytest.approx(0.30)


def test_aggregate_of_nothing_is_empty():
    assert aggregate([], METRICS) == {}


def test_by_source_groups_projections():
    out = project(
        [
            _cand("images.md", expected_lcp_reduction_pct=[20, 40]),
            _cand("caching.md", expected_ttfb_reduction_pct=[30, 80]),
        ],
        METRICS,
    )
    grouped = by_source(out)
    assert set(grouped) == {"images.md", "caching.md"}
    assert grouped["images.md"][0].metric == "lcp_ms"


def test_rank_key_orders_by_reduction_then_source_then_title():
    big = [Projection("lcp_ms", 6200, 5000, 4000, 0.20, "a.md")]
    small = [Projection("cls", 0.42, 0.40, 0.30, 0.05, "b.md")]
    assert rank_key("a.md", "Big win", big) < rank_key("b.md", "Small win", small)
    # no projections at all sorts last, deterministically
    assert rank_key("z.md", "Unknown", []) > rank_key("b.md", "Small win", small)
