"""Unit tests for analysis/reportmodel.py — assembly and determinism."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from analysis.estimator import Projection
from analysis.findings import Finding, Impact, PageAnalysis, Recommendation
from analysis.llm import LlmSummary
from analysis.reportmodel import (
    Report,
    build_report,
    campaign_id,
    stable_payload,
    to_json,
    verdict_for,
)
from config.load import Settings, Thresholds
from normalize.schema import Run
from rag import retrieve


def a_png_file(tmp_path, name="screenshot.png"):
    """A real 2x2 PNG on disk — Pillow writes it so the bytes are valid."""
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (2, 2), (255, 0, 0)).save(path, format="PNG")
    return str(path)


def a_har_file(tmp_path, *, sizes=(1000,), name="capture.har"):
    import json as _json

    entries = [
        {"time": 10.0,
         "request": {"url": f"https://example.com/{i}.js"},
         "response": {"status": 200, "_transferSize": size,
                      "content": {"mimeType": "text/javascript"}}}
        for i, size in enumerate(sizes)
    ]
    path = tmp_path / name
    path.write_text(_json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return str(path)


def make_run(run_id="run_a", page="homepage", lcp=6200, device="mid-mobile",
             network="slow-4g", screenshot="shot.png", har="capture.har"):
    return Run.model_validate({
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com"},
        "page": {"name": page, "url": f"https://example.com/{page}"},
        "condition": {"device": device, "network": network,
                      "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": "2026-01-08T14:30:00Z", "source": "automated"},
        "metrics": {
            "cwp": {"lcp_ms": lcp, "cls": 0.42, "inp_ms": 480, "fcp_ms": 3100,
                    "ttfb_ms": 1800, "tbt_ms": 620},
            "network": {"total_transfer_kb": 4820, "request_count": 118,
                        "render_blocking_css": 6},
            "main_thread": {"script_ms": 1820, "task_ms": 3100, "dom_nodes": 3200},
        },
        "resource_timings": [
            {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140,
             "duration_ms": 390},
            {"name": "/app.js", "type": "script", "transfer_kb": 480,
             "duration_ms": 120},
        ],
        "captures": {"screenshot": screenshot, "har": har},
    })


def a_page(name="homepage", recommendations=None, mode="llm", run_id="run_a",
           screenshot="shot.png", har="capture.har"):
    run = make_run(page=name, run_id=run_id, screenshot=screenshot, har=har)
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    recs = recommendations if recommendations is not None else [
        Recommendation(
            title="Serve modern formats", rationale="AVIF is smaller.",
            playbook_source="images.md", playbook_section="Serve modern formats",
            effort="low",
            projections=(Projection("lcp_ms", 6200, 5270, 3720, 0.15, "images.md"),),
        )
    ]
    return PageAnalysis(
        page_name=name, page_url=run.page.url, primary_run=run, runs=[run],
        symptoms=symptoms, summary=f"{name} is slow.",
        findings=[Finding(title="LCP too high", detail="6200ms",
                          evidence=("lcp_ms=6200",), symptom_codes=("lcp_fail",))],
        impacts=[Impact(audience="ux", text="Empty hero.")],
        recommendations=recs,
        projections={"lcp_ms": Projection("lcp_ms", 6200, 5270, 3720, 0.15,
                                          "aggregate")},
        mode=mode, degradation_reason=None if mode == "llm" else "no_api_key",
        dropped_recommendations=0, playbooks_cited=["images.md"],
    )


def a_summary():
    return LlmSummary(problem="Slow storefront.", key_finding="Media weight.",
                      top_actions=["Compress the hero", "Preload fonts"])


def build(pages=None, **kwargs):
    kwargs.setdefault("project", "storefront")
    kwargs.setdefault("settings", Settings())
    kwargs.setdefault("summary", a_summary())
    kwargs.setdefault("generated_at",
                      datetime(2026, 8, 2, 14, 30, tzinfo=timezone.utc))
    kwargs.setdefault("model", "gemini-2.0-flash")
    kwargs.setdefault("knowledge_digest", "abc123")
    return build_report(pages or [a_page()], **kwargs)


def a_report_with_captures(pages=("homepage",), screenshot="shot.png",
                            har="capture.har", **kwargs):
    """A Report whose captures point at the given paths.

    Builds through the same ``PageAnalysis`` construction ``a_page`` already
    uses, rather than a parallel path, so the appendix assembly sees exactly
    what production code sees.
    """
    page_analyses = [
        a_page(name=name, run_id=f"run_{name}", screenshot=screenshot, har=har)
        for name in pages
    ]
    return build(page_analyses, **kwargs)


# --------------------------------------------------------------------------- #
# campaign id
# --------------------------------------------------------------------------- #
def test_campaign_id_is_content_addressed_and_order_independent():
    assert campaign_id("storefront", ["b", "a"]) == campaign_id("storefront",
                                                                ["a", "b"])


def test_campaign_id_changes_with_the_runs():
    assert campaign_id("storefront", ["a"]) != campaign_id("storefront", ["a", "b"])


def test_campaign_id_slugifies_the_project_name():
    assert campaign_id("My Store!", ["a"]).startswith("my-store-")


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #
def test_verdict_is_the_worst_severity_present():
    run = make_run()
    fails = retrieve.detect_symptoms(run, Thresholds())
    assert verdict_for(fails) == "fail"
    assert verdict_for([]) == "pass"


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
def test_report_has_every_section_of_the_fixed_skeleton():
    report = build()
    assert isinstance(report, Report)
    assert report.schema_version == 2
    for section in ("cover", "summary", "pages", "comparison", "methodology", "meta"):
        assert getattr(report, section) is not None


def test_cover_lists_pages_and_the_worst_verdict():
    report = build([a_page("plp"), a_page("homepage")])
    assert report.cover.pages == ["homepage", "plp"]
    assert report.cover.verdict == "fail"
    assert report.cover.project == "storefront"


def test_pages_are_ordered_by_name():
    report = build([a_page("plp"), a_page("homepage"), a_page("pdp")])
    assert [p.name for p in report.pages] == ["homepage", "pdp", "plp"]


def test_recommendations_keep_the_order_findings_gave_them():
    high = Recommendation(
        title="Big", rationale="", playbook_source="a.md", playbook_section="s",
        effort="low",
        projections=(Projection("lcp_ms", 6200, 4000, 3000, 0.35, "a.md"),))
    low = Recommendation(
        title="Small", rationale="", playbook_source="b.md", playbook_section="s",
        effort="high",
        projections=(Projection("cls", 0.42, 0.40, 0.35, 0.05, "b.md"),))
    report = build([a_page(recommendations=[high, low])])
    assert [r.title for r in report.pages[0].recommendations] == ["Big", "Small"]


def test_a_recommendation_without_a_projection_is_marked_unknown():
    bare = Recommendation(title="Profile the origin", rationale="",
                          playbook_source="caching.md", playbook_section="s",
                          effort="medium", projections=())
    report = build([a_page(recommendations=[bare])])
    assert report.pages[0].recommendations[0].magnitude == "unknown"


def test_resources_are_ordered_heaviest_first():
    report = build()
    kb = [r.transfer_kb for r in report.pages[0].resources]
    assert kb == sorted(kb, reverse=True)


def test_resource_type_totals_are_summed():
    report = build()
    assert report.pages[0].resource_type_totals == {"media": 2140.0, "script": 480.0}


def test_comparison_row_per_condition_ordered():
    page = a_page()
    page.runs = [make_run("run_d", device="desktop", network="fast-3g"),
                 make_run("run_m", device="mid-mobile", network="slow-4g")]
    report = build([page])
    assert [(r.device, r.network) for r in report.comparison] == [
        ("desktop", "fast-3g"), ("mid-mobile", "slow-4g")
    ]


def test_comparison_verdict_is_per_condition_not_per_page():
    page = a_page()
    # Same page, one failing condition and one healthy one.
    healthy = make_run("run_d", device="desktop", network="fast-3g", lcp=1900)
    healthy.metrics.cwp.cls = 0.02
    healthy.metrics.cwp.inp_ms = 90
    healthy.metrics.cwp.fcp_ms = 1200
    healthy.metrics.cwp.ttfb_ms = 400
    healthy.metrics.cwp.tbt_ms = 50
    healthy.metrics.network.total_transfer_kb = 800
    healthy.metrics.network.request_count = 30
    healthy.metrics.network.render_blocking_css = 0
    healthy.metrics.main_thread.script_ms = 100
    healthy.resource_timings = []
    page.runs = [healthy, make_run("run_m")]

    report = build([page])
    rows = {r.device: r.verdict for r in report.comparison}
    assert rows["desktop"] == "pass"        # this condition really is fine
    assert rows["mid-mobile"] == "fail"
    # the page as a whole is still judged by its worst condition
    assert report.pages[0].verdict == "fail"


def test_meta_records_the_mode_and_cited_playbooks():
    report = build()
    assert report.meta.analysis_mode == "llm"
    assert report.meta.degradation_reason is None
    assert report.meta.playbooks_cited == ["images.md"]
    assert report.meta.knowledge_digest == "abc123"


def test_any_degraded_page_degrades_the_report_mode():
    report = build([a_page("homepage", mode="llm"), a_page("plp", mode="rule_based")])
    assert report.meta.analysis_mode == "rule_based"
    assert report.meta.degradation_reason == "no_api_key"


def test_methodology_lists_devices_networks_and_captures():
    report = build()
    assert report.methodology.devices == ["mid-mobile"]
    assert report.methodology.networks == ["slow-4g"]
    assert report.methodology.captures[0].screenshot == "shot.png"


def test_thresholds_come_from_settings_not_hard_coded():
    settings = Settings()
    settings.thresholds.lcp_good_ms = 1234
    report = build(settings=settings)
    assert report.methodology.thresholds["lcp_good_ms"] == 1234
    assert report.pages[0].targets["lcp_ms"] == 1234


# --------------------------------------------------------------------------- #
# serialisation
# --------------------------------------------------------------------------- #
def test_to_json_round_trips():
    payload = json.loads(to_json(build()))
    assert payload["schema_version"] == 2
    assert payload["cover"]["campaign_id"]


def test_stable_payload_drops_generated_at():
    early = build(generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    late = build(generated_at=datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert stable_payload(early) == stable_payload(late)
    assert to_json(early) != to_json(late)


# --------------------------------------------------------------------------- #
# appendix (Phase 7B)
# --------------------------------------------------------------------------- #
def test_appendix_carries_one_entry_per_capture_ordered_by_page_and_run():
    report = a_report_with_captures(pages=("plp", "homepage"))
    assert [(e.page, e.run_id) for e in report.appendix] == [
        ("homepage", "run_homepage"), ("plp", "run_plp"),
    ]


def test_appendix_entries_carry_the_condition_they_were_captured_under():
    entry = a_report_with_captures().appendix[0]
    assert (entry.device, entry.network) == ("mid-mobile", "slow-4g")


def test_appendix_holds_paths_never_image_bytes():
    # report.json must stay a text document a human can read and git can diff.
    payload = to_json(a_report_with_captures())
    assert "base64" not in payload
    assert "data:image" not in payload


def test_appendix_rows_come_from_the_har(tmp_path):
    report = a_report_with_captures(har=a_har_file(tmp_path, sizes=(9000, 10)))
    entry = report.appendix[0]
    assert [r.transfer_bytes for r in entry.requests] == [9000, 10]
    assert entry.total_requests == 2
    assert entry.total_transfer_bytes == 9010


def test_a_capture_with_no_artifacts_degrades_without_dropping_the_entry():
    report = a_report_with_captures(screenshot=None, har=None)
    assert len(report.appendix) == 1
    assert report.appendix[0].degraded == [
        "screenshot not retained", "HAR not retained",
    ]


def test_degraded_entries_are_counted_in_meta():
    report = a_report_with_captures(screenshot=None, har=None)
    assert report.meta.degraded_appendix_entries == 1


def test_a_clean_capture_counts_as_zero_degraded(tmp_path):
    report = a_report_with_captures(
        screenshot=a_png_file(tmp_path), har=a_har_file(tmp_path)
    )
    assert report.appendix[0].degraded == []
    assert report.meta.degraded_appendix_entries == 0


def test_methodology_captures_are_unchanged_by_the_appendix():
    report = a_report_with_captures()
    assert [c.run_id for c in report.methodology.captures] == ["run_homepage"]


def test_schema_version_records_the_appendix_addition():
    assert a_report_with_captures().schema_version == 2


def test_appendix_breaks_ties_on_device_and_network_when_run_ids_collide():
    # load_runs reads data/processed/*.json directly with no run_id
    # uniqueness constraint (normalize/schema.py only requires min_length=1),
    # so two runs of the same page can legitimately share a run_id. Without
    # device/network in the sort key, a tie falls back to input position,
    # which this project's determinism rules forbid.
    page = a_page()
    page.runs = [
        make_run("run_x", device="mid-mobile", network="slow-4g"),
        make_run("run_x", device="desktop", network="fast-3g"),
    ]
    report = build([page])
    assert [(e.device, e.network) for e in report.appendix] == [
        ("desktop", "fast-3g"), ("mid-mobile", "slow-4g"),
    ]
