"""Unit tests for report/render_html.py.

Rendering is where model-authored prose first becomes markup, so escaping is
tested as a security property, not a nicety.
"""
from __future__ import annotations

import re
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
             finding_title="Hero video is the LCP element", trends=(),
             appendix=None):
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
        "schema_version": 2,
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
        # `None` (the default) derives one realistic entry per page, the same
        # way `methodology.captures` below already does — a real campaign's
        # appendix is never actually empty, since every run produces one
        # entry (see `analysis.reportmodel._appendix`). Passing `appendix=[]`
        # explicitly still exercises the genuinely-empty-appendix path.
        "appendix": (list(appendix) if appendix is not None else [
            {"page": p, "run_id": f"run_{p}", "device": "mid-mobile",
             "network": "slow-4g", "screenshot": "shot.png",
             "har": None, "har_sha256": None, "har_bytes": None,
             "requests": [], "total_requests": 0, "total_transfer_bytes": 0,
             "degraded": []} for p in pages
        ]),
        "methodology": {"devices": ["mid-mobile"], "networks": ["slow-4g"],
                        "runs_per_condition": [3],
                        "captures": ([{"page": pages[0], "run_id": f"run_{pages[0]}",
                                       "device": "mid-mobile", "network": "slow-4g",
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


_UNSET = object()


def an_appendix_entry(run_id="run_homepage", *, screenshot="data/raw/s.png",
                      requests=True, degraded=(), request_rows=None,
                      total_transfer_bytes=_UNSET):
    default_rows = [
        {"url": "https://example.com/hero.mp4", "resource_type": "media",
         "status": 200, "transfer_bytes": 4_200_000, "duration_ms": 3100.0},
    ] if requests else []
    rows = default_rows if request_rows is None else list(request_rows)
    if total_transfer_bytes is _UNSET:
        total_transfer_bytes = 8_100_000 if requests else 0
    return {
        "page": "homepage", "run_id": run_id,
        "device": "mid-mobile", "network": "slow-4g",
        "screenshot": screenshot, "har": "data/raw/capture.har",
        "har_sha256": "a" * 64, "har_bytes": 4096,
        "requests": rows,
        "total_requests": 214 if requests else 0,
        "total_transfer_bytes": total_transfer_bytes,
        "degraded": list(degraded),
    }


def test_the_appendix_renders_an_entry_per_capture():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    html = render_html(report)
    assert html.count('data-section="capture.screenshot"') == 1
    assert html.count('data-section="capture.requests"') == 1


def test_the_request_table_shows_the_url_and_its_transfer_size():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    html = render_html(report)
    assert "https://example.com/hero.mp4" in html
    assert "4.0 MB" in html  # 4_200_000 bytes / 1024 / 1024 = 4.005…


def test_the_true_request_count_is_stated_beside_the_truncated_table():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    assert "214" in render_html(report)


def test_unknown_and_zero_transfer_size_render_differently_in_the_same_table():
    # The whole point of the fix: a request whose size was never recorded
    # must not read the same as a request that genuinely transferred zero
    # bytes. Both rows sit in the same table so the two dashes/values can be
    # told apart by more than test isolation.
    report = Report.model_validate(a_report(appendix=[an_appendix_entry(
        request_rows=[
            {"url": "https://example.com/cached.js", "resource_type": "script",
             "status": 304, "transfer_bytes": 0, "duration_ms": 5.0},
            {"url": "https://example.com/unmeasured.js", "resource_type": "script",
             "status": 200, "transfer_bytes": None, "duration_ms": 12.0},
        ],
        total_transfer_bytes=0,
    )]))
    html = render_html(report)
    rows = re.findall(r"<tr>.*?</tr>", html, flags=re.S)
    cached_row = next(r for r in rows if "cached.js" in r)
    unmeasured_row = next(r for r in rows if "unmeasured.js" in r)
    assert "<td>0 B</td>" in cached_row
    assert "<td>—</td>" in unmeasured_row
    assert "0 B" not in unmeasured_row
    assert "—</td>" not in cached_row


def test_an_unknown_total_reads_sensibly_rather_than_a_bare_dash():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry(
        total_transfer_bytes=None,
    )]))
    html = render_html(report)
    assert "total transferred size unknown" in html
    assert "— transferred in total" not in html


def test_a_screenshot_is_embedded_when_an_image_is_supplied():
    from report.images import EmbeddedImage, entry_key

    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    entry = report.appendix[0]
    html = render_html(report, images={
        entry_key(entry): EmbeddedImage(data_uri="data:image/png;base64,AAAA",
                                      width=720, height=450, cropped=False),
    })
    assert "data:image/png;base64,AAAA" in html


def test_a_cropped_screenshot_says_so_in_the_caption():
    from report.images import EmbeddedImage, entry_key

    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    entry = report.appendix[0]
    html = render_html(report, images={
        entry_key(entry): EmbeddedImage(data_uri="data:image/png;base64,AAAA",
                                      width=720, height=1600, cropped=True),
    })
    assert "Top 1600" in html


def test_without_images_the_figure_renders_its_empty_state_not_a_broken_img():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    html = render_html(report)
    assert 'data-section="capture.screenshot"' in html
    assert "data:image" not in html


def test_a_capture_with_no_requests_still_renders_the_table_block():
    report = Report.model_validate(
        a_report(appendix=[an_appendix_entry(requests=False,
                                             degraded=["HAR not retained"])])
    )
    html = render_html(report)
    assert 'data-section="capture.requests"' in html
    assert "HAR not retained" in html


def test_an_empty_appendix_still_renders_the_section():
    # No section is ever conditionally omitted.
    html = render_html(Report.model_validate(a_report(appendix=[])))
    assert 'data-section="appendix"' in html


def test_an_empty_appendix_shares_a_fingerprint_with_a_populated_one():
    # `Report.appendix` defaults to `[]`, and every report.json written
    # before this branch loads fine and renders an empty appendix — so this
    # is the state `--skeleton-check` sees on every archived campaign. The
    # empty state must carry the same capture[] / capture.screenshot /
    # capture.requests fingerprint as a populated one, or the drift guard
    # fires a false positive on every pre-existing report.
    empty = Report.model_validate(a_report(appendix=[]))
    populated = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    empty_fp = fingerprint(render_html(empty))
    populated_fp = fingerprint(render_html(populated))
    assert empty_fp == populated_fp
    assert empty_fp[-4:] == [
        "appendix", "capture[]", "capture.screenshot", "capture.requests",
    ]


def test_a_one_capture_and_a_six_capture_report_share_a_fingerprint():
    one = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    six = Report.model_validate(a_report(appendix=[
        an_appendix_entry(run_id=f"run_{i}") for i in range(6)
    ]))
    one_fp = fingerprint(render_html(one))
    six_fp = fingerprint(render_html(six))
    assert one_fp == six_fp
    # Equality alone isn't enough: both sides degrading to the same *empty*
    # group would satisfy this too (see skeleton_test.py's sibling assertion
    # at the same fixture shape). Assert the capture children actually
    # survived the collapse.
    assert one_fp[-4:] == [
        "appendix", "capture[]", "capture.screenshot", "capture.requests",
    ]


# --- LCP lower-bound caveat ------------------------------------------------ #
# When the largest LCP candidate was a cross-origin resource with no
# Timing-Allow-Origin, the browser exposes no time for it and `lcp_ms` is the
# largest *timed* element. The report must say so, or a 1165ms lower bound
# reads as a comfortable pass.

def _flag_lcp(report, flagged=True):
    for page in report.pages:
        page.conditions[0].lcp_underestimated = flagged
    return report


def test_html_marks_lcp_as_a_lower_bound_when_flagged():
    report = _flag_lcp(a_report())
    html = render_html(report)
    assert "lower bound" in html
    assert "Timing-Allow-Origin" in html


def test_html_omits_lcp_caveat_when_not_flagged():
    report = _flag_lcp(a_report(), flagged=False)
    html = render_html(report)
    assert "Timing-Allow-Origin" not in html


def test_lcp_caveat_does_not_drift_the_skeleton():
    """The caveat lives inside page.cwv-dashboard — it adds no new section."""
    flagged = _flag_lcp(a_report())
    plain = _flag_lcp(a_report(), flagged=False)
    assert fingerprint(render_html(flagged)) == \
           fingerprint(render_html(plain))
