"""Unit tests for report/render_html.py.

Rendering is where model-authored prose first becomes markup, so escaping is
tested as a security property, not a nicety.
"""
from __future__ import annotations

from datetime import datetime, timezone

from analysis.reportmodel import Report
from report.render_html import build_charts, render_html
from report.skeleton import fingerprint


def a_trend(metric="lcp_ms", *, values=(6200.0, 4820.0), direction="improved",
            delta_pct=-22.3, target=2500.0, crossed=None):
    return {
        "page": "homepage", "device": "mid-mobile", "network": "slow-4g",
        "metric": metric, "direction": direction, "delta_pct": delta_pct,
        "target": target, "crossed": crossed,
        "points": [{"run_id": f"run_{i}", "value": v, "at": "2026-08-04"}
                   for i, v in enumerate(values)],
    }


def a_report(pages=("homepage",), *, recommendations=True, mode="llm",
             finding_title="Hero video is the LCP element", trends=()):
    def page_block(name):
        return {
            "trends": list(trends),
            "name": name,
            "url": f"https://example.com/{name}",
            "primary_run_id": f"run_{name}",
            "verdict": "fail",
            "conditions": [{
                "run_id": f"run_{name}", "device": "mid-mobile",
                "network": "slow-4g", "cpu_throttle": 4, "runs": 3,
                "metrics": {"lcp_ms": 6200, "cls": 0.42, "inp_ms": 480,
                            "fcp_ms": 3100, "ttfb_ms": 1800, "tbt_ms": 620},
            }],
            "metrics": {
                "cwp": {"lcp_ms": 6200, "cls": 0.42, "inp_ms": 480,
                        "fcp_ms": 3100, "ttfb_ms": 1800, "tbt_ms": 620},
                "lighthouse": {"performance": 54},
                "network": {"total_transfer_kb": 4820, "request_count": 118,
                            "render_blocking_css": 6},
                "main_thread": {"script_ms": 1820, "task_ms": 3100},
            },
            "targets": {"lcp_ms": 2500, "cls": 0.1, "inp_ms": 200,
                        "fcp_ms": 1800, "ttfb_ms": 800},
            "symptoms": [{"code": "lcp_fail", "text": "LCP is 6200ms.",
                          "severity": "fail", "metric": "lcp_ms",
                          "value": 6200, "target": 2500}],
            "resources": [{"name": "/hero.mp4", "type": "media",
                           "transfer_kb": 2140, "duration_ms": 390}],
            "resource_type_totals": {"media": 2140.0},
            "summary": f"{name} is slow.",
            "findings": [{"title": finding_title, "detail": "2140KB.",
                          "evidence": ["lcp_ms=6200"],
                          "symptom_codes": ["lcp_fail"]}],
            "impacts": [{"audience": "ux", "text": "Empty hero."}],
            "recommendations": ([{
                "title": "Compress the hero", "rationale": "Fewer bytes.",
                "playbook_source": "images.md",
                "playbook_section": "Serve modern formats",
                "effort": "low", "magnitude": "estimated",
                "projections": [{"metric": "lcp_ms", "before": 6200.0,
                                 "after_low": 5270.0, "after_high": 3720.0,
                                 "reduction_pct": 0.15, "source": "images.md"}],
            }] if recommendations else []),
            "projections": ({"lcp_ms": {"metric": "lcp_ms", "before": 6200.0,
                                        "after_low": 5270.0, "after_high": 3720.0,
                                        "reduction_pct": 0.15,
                                        "source": "aggregate"}}
                            if recommendations else {}),
        }

    return Report.model_validate({
        "schema_version": 1,
        "cover": {"project": "storefront", "campaign_id": "storefront-abc12345",
                  "generated_at": datetime(2026, 8, 2, 14, 30, tzinfo=timezone.utc),
                  "pages": list(pages), "verdict": "fail"},
        "summary": {"problem": "Storefront is slow.",
                    "key_finding": "Media weight dominates.",
                    "top_actions": ["Compress the hero"]},
        "pages": [page_block(p) for p in pages],
        "comparison": [{"page": p, "device": "mid-mobile", "network": "slow-4g",
                        "lcp_ms": 6200, "cls": 0.42, "inp_ms": 480,
                        "tbt_ms": 620, "verdict": "fail"} for p in pages],
        "methodology": {"devices": ["mid-mobile"], "networks": ["slow-4g"],
                        "runs_per_condition": [3],
                        "captures": ([{"page": pages[0], "run_id": f"run_{pages[0]}",
                                       "screenshot": "shot.png"}] if pages else []),
                        "thresholds": {"lcp_good_ms": 2500, "lcp_fail_ms": 4000,
                                       "cls_good": 0.1, "cls_fail": 0.25,
                                       "inp_good_ms": 200, "inp_fail_ms": 500,
                                       "fcp_good_ms": 1800, "ttfb_good_ms": 800}},
        "meta": {"analysis_mode": mode,
                 "degradation_reason": None if mode == "llm" else "no_api_key",
                 "model": "gemini-2.0-flash", "playbooks_cited": ["images.md"],
                 "dropped_recommendations": 0, "knowledge_digest": "abc"},
    })


def test_renders_a_complete_html_document():
    html = render_html(a_report())
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_the_stylesheet_is_inlined_so_the_file_is_self_contained():
    html = render_html(a_report())
    assert "<style>" in html
    assert "@page" in html
    assert "<link" not in html


def test_the_inlined_stylesheet_is_not_html_escaped():
    # Autoescape would turn `"Segoe UI"` into `&quot;Segoe UI&quot;`, which
    # breaks the custom property and silently drops the whole type system back
    # to Times New Roman. The stylesheet is our own file, not model output.
    html = render_html(a_report())
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "&quot;" not in style
    assert "&#" not in style
    assert '"Segoe UI"' in style
    assert "--sans:" in style


def test_cover_shows_project_verdict_and_pages():
    html = render_html(a_report(("homepage", "plp")))
    assert "storefront" in html
    assert "storefront-abc12345" in html
    assert "homepage" in html and "plp" in html


def test_charts_are_embedded_as_inline_svg():
    html = render_html(a_report())
    assert "<svg" in html
    assert "&lt;svg" not in html  # not escaped into visible text


def test_model_authored_prose_is_escaped():
    hostile = '<script>alert(1)</script>'
    html = render_html(a_report(finding_title=hostile))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_recommendations_render_an_empty_state_not_a_missing_section():
    html = render_html(a_report(recommendations=False))
    assert 'data-section="page.recommendations"' in html
    assert "No playbook-grounded recommendations" in html


def test_a_rule_based_report_says_so_on_the_cover():
    html = render_html(a_report(mode="rule_based"))
    assert "rule-based" in html.lower()


def test_an_llm_report_does_not_claim_degradation():
    html = render_html(a_report(mode="llm"))
    assert "no_api_key" not in html


def test_a_page_with_no_history_renders_the_trend_empty_state():
    html = render_html(a_report())
    assert 'data-section="page.trend"' in html
    assert "No prior campaigns" in html


def test_a_trend_states_its_direction_and_change():
    html = render_html(a_report(trends=[a_trend()]))
    assert "improved" in html
    assert "-22.3%" in html


def test_a_trend_caption_names_the_metric_the_way_its_chart_does():
    # Raw field names in the caption beside an axis reading "LCP" would read
    # as two different things being shown.
    html = render_html(a_report(trends=[a_trend()]))
    assert ">LCP<" in html
    assert ">lcp_ms<" not in html


def test_a_target_crossing_is_shown():
    html = render_html(a_report(trends=[a_trend(crossed="into_fail")]))
    assert "into_fail" in html


def test_a_first_campaign_series_says_so_rather_than_drawing_a_line():
    html = render_html(a_report(trends=[a_trend(values=(4820.0,),
                                                direction="new",
                                                delta_pct=None)]))
    assert "First campaign for this condition" in html


def test_the_direction_classes_let_the_stylesheet_colour_the_caption():
    html = render_html(a_report(trends=[a_trend(direction="regressed")]))
    assert "trend--regressed" in html


def test_the_skeleton_survives_a_page_with_no_history():
    # The trend section is unconditional, exactly like every other block.
    with_history = fingerprint(render_html(a_report(trends=[a_trend()])))
    without = fingerprint(render_html(a_report()))
    assert with_history == without


def test_the_skeleton_is_identical_across_campaign_sizes():
    one = fingerprint(render_html(a_report(("homepage",))))
    three = fingerprint(render_html(a_report(("homepage", "pdp", "plp"))))
    assert one == three


def test_the_skeleton_survives_a_page_with_no_recommendations():
    full = fingerprint(render_html(a_report()))
    bare = fingerprint(render_html(a_report(recommendations=False)))
    assert full == bare


def test_rendering_is_a_pure_function():
    report = a_report()
    assert render_html(report) == render_html(report)


def test_build_charts_returns_one_entry_per_page():
    charts = build_charts(a_report(("homepage", "plp")))
    assert set(charts["pages"]) == {"homepage", "plp"}
    assert "gauges" in charts["pages"]["homepage"]
    assert charts["comparison"]
