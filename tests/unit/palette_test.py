"""Unit tests for report/palette.py.

The palette is the single place that decides what "fail" looks like. If a
chart or a badge ever picks a colour of its own, the report stops being
comparable run to run - so these tests pin both the classification rule and
the token mapping.
"""
from __future__ import annotations

from report import palette

THRESHOLDS = {
    "lcp_good_ms": 2500, "lcp_fail_ms": 4000,
    "cls_good": 0.1, "cls_fail": 0.25,
    "inp_good_ms": 200, "inp_fail_ms": 500,
    "fcp_good_ms": 1800, "ttfb_good_ms": 800,
}


def test_value_below_good_is_a_pass():
    assert palette.classify("lcp_ms", 1800, THRESHOLDS) == "pass"


def test_value_exactly_at_the_good_threshold_is_a_pass():
    # 2500ms is the boundary of "good" - inclusive, not the start of "warn".
    assert palette.classify("lcp_ms", 2500, THRESHOLDS) == "pass"


def test_value_between_good_and_fail_is_a_warn():
    assert palette.classify("lcp_ms", 3000, THRESHOLDS) == "warn"


def test_value_exactly_at_the_fail_threshold_is_a_warn():
    assert palette.classify("lcp_ms", 4000, THRESHOLDS) == "warn"


def test_value_above_fail_is_a_fail():
    assert palette.classify("lcp_ms", 6200, THRESHOLDS) == "fail"


def test_cls_uses_its_own_unitless_thresholds():
    assert palette.classify("cls", 0.05, THRESHOLDS) == "pass"
    assert palette.classify("cls", 0.18, THRESHOLDS) == "warn"
    assert palette.classify("cls", 0.42, THRESHOLDS) == "fail"


def test_inp_is_classified():
    assert palette.classify("inp_ms", 120, THRESHOLDS) == "pass"
    assert palette.classify("inp_ms", 480, THRESHOLDS) == "warn"
    assert palette.classify("inp_ms", 900, THRESHOLDS) == "fail"


def test_a_metric_with_only_a_good_threshold_never_reports_fail():
    # ttfb has no configured fail threshold - warn is as bad as it gets.
    assert palette.classify("ttfb_ms", 400, THRESHOLDS) == "pass"
    assert palette.classify("ttfb_ms", 1800, THRESHOLDS) == "warn"


def test_missing_value_is_unknown():
    assert palette.classify("lcp_ms", None, THRESHOLDS) == "unknown"


def test_unknown_metric_is_unknown():
    assert palette.classify("nonsense_ms", 100, THRESHOLDS) == "unknown"


def test_missing_threshold_is_unknown():
    assert palette.classify("lcp_ms", 100, {}) == "unknown"


def test_colour_mapping_is_total():
    for verdict in ("pass", "warn", "fail", "unknown"):
        colour = palette.colour_for(verdict)
        assert colour.startswith("#") and len(colour) == 7


def test_an_unrecognised_verdict_falls_back_rather_than_raising():
    assert palette.colour_for("banana") == palette.UNKNOWN


def test_categorical_colours_cycle_and_are_stable():
    first = [palette.categorical_for(i) for i in range(len(palette.CATEGORICAL))]
    assert len(set(first)) == len(palette.CATEGORICAL)
    # wraps rather than raising, and wraps to the same value every time
    assert palette.categorical_for(len(palette.CATEGORICAL)) == palette.CATEGORICAL[0]
