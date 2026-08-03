"""Unit tests for report/charts.py.

Charts are pure functions returning SVG *text*, which is the reason this file
can assert what a chart shows - bar count, labels, the fail-red on the failing
metric - instead of only that some image exists.
"""
from __future__ import annotations

import re

from report import charts, palette

THRESHOLDS = {
    "lcp_good_ms": 2500, "lcp_fail_ms": 4000,
    "cls_good": 0.1, "cls_fail": 0.25,
    "inp_good_ms": 200, "inp_fail_ms": 500,
    "fcp_good_ms": 1800, "ttfb_good_ms": 800,
}

FAILING = {"lcp_ms": 6200, "cls": 0.42, "inp_ms": 480}
HEALTHY = {"lcp_ms": 1800, "cls": 0.02, "inp_ms": 90}


def svg_text(svg: str) -> str:
    """All text content in an SVG, whitespace-collapsed, for label assertions."""
    return " ".join(re.findall(r">([^<>]+)<", svg))


def test_to_svg_strips_the_xml_prologue_so_it_can_be_inlined():
    svg = charts.cwv_gauges(FAILING, THRESHOLDS)
    assert not svg.lstrip().startswith("<?xml")
    assert "<!DOCTYPE" not in svg
    assert svg.lstrip().startswith("<svg")


def test_gauges_render_one_panel_per_core_metric():
    svg = charts.cwv_gauges(FAILING, THRESHOLDS)
    text = svg_text(svg)
    assert "LCP" in text
    assert "CLS" in text
    assert "INP" in text


def test_gauges_colour_a_failing_metric_with_the_fail_token():
    svg = charts.cwv_gauges(FAILING, THRESHOLDS)
    assert palette.FAIL.lower() in svg.lower()


def test_gauges_colour_a_passing_metric_with_the_pass_token():
    svg = charts.cwv_gauges(HEALTHY, THRESHOLDS)
    assert palette.PASS.lower() in svg.lower()
    assert palette.FAIL.lower() not in svg.lower()


def test_gauges_with_no_metrics_at_all_return_the_empty_marker():
    assert charts.cwv_gauges({}, THRESHOLDS) == charts.NO_CHART


def test_gauges_render_the_measured_metrics_when_one_is_missing():
    svg = charts.cwv_gauges({"lcp_ms": 6200, "cls": None, "inp_ms": None}, THRESHOLDS)
    assert svg != charts.NO_CHART
    assert "LCP" in svg_text(svg)


def test_the_same_input_renders_byte_identical_svg():
    first = charts.cwv_gauges(FAILING, THRESHOLDS)
    second = charts.cwv_gauges(FAILING, THRESHOLDS)
    assert first == second


def test_svg_carries_no_timestamp():
    svg = charts.cwv_gauges(FAILING, THRESHOLDS)
    assert "<dc:date>" not in svg
