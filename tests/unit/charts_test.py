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


# --------------------------------------------------------------------------- #
# The remaining five builders
# --------------------------------------------------------------------------- #
RESOURCES = [
    {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140.0, "duration_ms": 390},
    {"name": "/app.js", "type": "script", "transfer_kb": 480.0, "duration_ms": 120},
    {"name": "/brand.woff2", "type": "font", "transfer_kb": 92.0, "duration_ms": 60},
]
TOTALS = {"media": 2140.0, "script": 480.0, "font": 92.0}
CWP = {"ttfb_ms": 1800.0, "fcp_ms": 3100.0, "lcp_ms": 6200.0}
PROJECTIONS = {
    "lcp_ms": {"metric": "lcp_ms", "before": 6200.0, "after_low": 4637.6,
               "after_high": 2529.6, "reduction_pct": 0.25, "source": "aggregate"},
    "ttfb_ms": {"metric": "ttfb_ms", "before": 1800.0, "after_low": 957.6,
                "after_high": 540.0, "reduction_pct": 0.47, "source": "aggregate"},
}
COMPARISON = [
    {"page": "homepage", "device": "desktop", "network": "fast-3g",
     "lcp_ms": 2400.0, "cls": 0.05, "inp_ms": 120.0, "tbt_ms": 120.0,
     "verdict": "warn"},
    {"page": "homepage", "device": "mid-mobile", "network": "slow-4g",
     "lcp_ms": 6200.0, "cls": 0.42, "inp_ms": 480.0, "tbt_ms": 620.0,
     "verdict": "fail"},
]


# -- resource_bars ---------------------------------------------------------- #
def test_resource_bars_render_one_bar_per_resource_heaviest_first():
    svg = charts.resource_bars(RESOURCES)
    text = svg_text(svg)
    assert "hero.mp4" in text
    assert "app.js" in text
    # heaviest label sits above the lighter ones in the axis order
    assert text.index("hero.mp4") < text.index("brand.woff2")


def test_resource_bars_truncate_long_names():
    long_name = "/" + ("a" * 200) + "/bundle.js"
    svg = charts.resource_bars([
        {"name": long_name, "type": "script", "transfer_kb": 10.0, "duration_ms": 5}
    ])
    assert "a" * 200 not in svg_text(svg)


def test_resource_bars_cap_the_number_of_rows():
    many = [
        {"name": f"/asset-{i}.js", "type": "script",
         "transfer_kb": float(100 - i), "duration_ms": 10}
        for i in range(30)
    ]
    text = svg_text(charts.resource_bars(many, limit=5))
    assert "asset-0.js" in text
    assert "asset-20.js" not in text


def test_resource_bars_with_no_resources_return_the_empty_marker():
    assert charts.resource_bars([]) == charts.NO_CHART


def test_resource_bars_with_only_zero_byte_resources_return_the_empty_marker():
    zero = [{"name": "/x.js", "type": "script", "transfer_kb": 0.0,
             "duration_ms": 1}]
    assert charts.resource_bars(zero) == charts.NO_CHART


# -- request_type_donut ----------------------------------------------------- #
def test_donut_renders_each_type():
    text = svg_text(charts.request_type_donut(TOTALS))
    assert "media" in text
    assert "script" in text


def test_donut_with_a_single_type_still_renders():
    svg = charts.request_type_donut({"script": 400.0})
    assert svg != charts.NO_CHART
    assert "script" in svg_text(svg)


def test_donut_with_no_bytes_returns_the_empty_marker():
    assert charts.request_type_donut({}) == charts.NO_CHART
    assert charts.request_type_donut({"script": 0.0}) == charts.NO_CHART


# -- lcp_phases ------------------------------------------------------------- #
def test_lcp_phases_render_three_phases_with_the_derivation_caption():
    svg = charts.lcp_phases(CWP)
    text = svg_text(svg)
    assert "Server" in text
    assert "Render-blocking" in text
    assert "LCP element" in text
    assert "paint milestones" in text.lower()


def test_lcp_phases_without_fcp_return_the_empty_marker():
    assert charts.lcp_phases({"ttfb_ms": 1800.0, "lcp_ms": 6200.0}) == charts.NO_CHART


def test_lcp_phases_without_ttfb_return_the_empty_marker():
    assert charts.lcp_phases({"fcp_ms": 3100.0, "lcp_ms": 6200.0}) == charts.NO_CHART


def test_lcp_phases_with_a_negative_derived_phase_return_the_empty_marker():
    # FCP after LCP happens on odd runs; the arithmetic would draw a lie.
    odd = {"ttfb_ms": 1800.0, "fcp_ms": 7000.0, "lcp_ms": 6200.0}
    assert charts.lcp_phases(odd) == charts.NO_CHART


# -- projection_bars -------------------------------------------------------- #
def test_projection_bars_show_before_and_after_per_metric():
    text = svg_text(charts.projection_bars(PROJECTIONS))
    assert "LCP" in text
    assert "TTFB" in text


def test_projection_bars_with_nothing_projected_return_the_empty_marker():
    assert charts.projection_bars({}) == charts.NO_CHART


# -- comparison_heat -------------------------------------------------------- #
def test_comparison_heat_renders_a_row_per_condition():
    text = svg_text(charts.comparison_heat(COMPARISON))
    assert "desktop" in text
    assert "mid-mobile" in text


def test_comparison_heat_with_no_rows_returns_the_empty_marker():
    assert charts.comparison_heat([]) == charts.NO_CHART


# -- trend_chart ------------------------------------------------------------ #
def a_series(values=(6200.0, 5940.0, 4820.0), *, metric="lcp_ms",
             direction="improved", target=2500.0):
    return {
        "page": "homepage", "device": "mid-mobile", "network": "slow-4g",
        "metric": metric, "direction": direction, "delta_pct": -18.9,
        "target": target,
        "points": [{"run_id": f"run_{i}", "value": v, "at": "2026-08-04"}
                   for i, v in enumerate(values)],
    }


def test_trend_chart_labels_the_metric_and_the_latest_value():
    text = svg_text(charts.trend_chart(a_series()))
    assert "LCP" in text
    assert "4820 ms" in text


def test_trend_chart_marks_the_configured_target():
    assert "target 2500 ms" in svg_text(charts.trend_chart(a_series()))


def test_trend_chart_omits_the_target_line_when_none_is_configured():
    text = svg_text(charts.trend_chart(a_series(metric="tbt_ms", target=None)))
    assert "target" not in text


def test_the_newest_point_carries_the_direction_colour():
    regressed = charts.trend_chart(a_series(direction="regressed"))
    improved = charts.trend_chart(a_series(direction="improved"))
    assert palette.FAIL in regressed
    assert palette.PASS in improved


def test_a_flat_trend_is_not_painted_as_an_improvement():
    # "flat" is the absence of a signal, not good news.
    flat = charts.trend_chart(a_series(direction="flat"))
    assert palette.PASS not in flat


def test_a_single_point_series_refuses_to_draw():
    # A line through one point states a trend that has not been measured yet.
    assert charts.trend_chart(a_series((4820.0,))) == charts.NO_CHART


def test_a_series_with_no_points_refuses_to_draw():
    assert charts.trend_chart(a_series(())) == charts.NO_CHART


# -- determinism across every builder --------------------------------------- #
def test_every_builder_is_deterministic():
    pairs = [
        (charts.resource_bars, (RESOURCES,)),
        (charts.request_type_donut, (TOTALS,)),
        (charts.lcp_phases, (CWP,)),
        (charts.projection_bars, (PROJECTIONS,)),
        (charts.comparison_heat, (COMPARISON,)),
        (charts.trend_chart, (a_series(),)),
    ]
    for builder, args in pairs:
        assert builder(*args) == builder(*args), builder.__name__
