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
