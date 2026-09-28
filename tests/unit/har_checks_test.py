"""Unit tests for analysis/har_checks.py.

The HARs are synthetic, built in the shape of the WebPageTest exports the checks
were written against: real ones carry cookies and cannot be committed. Each
fixture reproduces one pattern seen in the live Oakley captures (2026-09-17).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from analysis import har_checks as hc

T0 = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)
HERO = "https://media.oakley.com/cms/hero/portrait_ratio375x600/750/hero-m.jpg"
PDP_IMG = "https://assets2.oakley.com/p/0OO9102__qt.png?impolicy=OO_ratio&width={w}"


def entry(url, start, end, *, page="page_1", ctype="image/jpeg", status=200,
          initiator_type="parser", initiator=None, request_type="Image",
          render_blocking=None, priority="High"):
    return {
        "pageref": page, "startedDateTime": (T0 + timedelta(milliseconds=start)).isoformat(),
        "time": end - start, "request": {"url": url, "method": "GET", "headers": []},
        "response": {"status": status, "headers": []}, "_load_start": start, "_all_end": end,
        "_responseCode": status, "_contentType": ctype, "_initiator_type": initiator_type,
        "_initiator": initiator, "_request_type": request_type,
        "_renderBlocking": render_blocking, "_priority": priority,
    }


def page(pid, *, lcp=None, lcp_url=None, cls=0.0, shifts=(), console=()):
    return {"id": pid, "startedDateTime": T0.isoformat(), "_URL": "https://www.oakley.com/en-us",
            "_chromeUserTiming.LargestContentfulPaint": lcp,
            "_LargestContentfulPaintImageURL": lcp_url, "_LargestContentfulPaintType": "image",
            "_chromeUserTiming.CumulativeLayoutShift": cls,
            "_LayoutShifts": json.dumps(list(shifts)), "_consoleLog": list(console)}


def write_har(tmp_path, pages, entries, name="TEST.har"):
    path = tmp_path / name
    path.write_text(json.dumps({"log": {"creator": {"name": "WebPagetest"},
                                        "pages": pages, "entries": entries}}),
                    encoding="utf-8")
    return hc.load_har(path)


def three_runs(tmp_path, build):
    """Three page loads, each built by ``build(pid)`` -> (page, entries)."""
    pages, entries = [], []
    for n in (1, 2, 3):
        p, e = build(f"page_{n}")
        pages.append(p)
        entries += e
    return write_har(tmp_path, pages, entries)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def test_each_page_in_the_har_is_one_run_and_preflights_are_dropped(tmp_path):
    def build(pid):
        return page(pid, lcp=2000, lcp_url=HERO), [
            entry("https://www.oakley.com/en-us", 0, 400, page=pid, ctype="text/html"),
            entry(HERO, 700, 800, page=pid, request_type="Preflight",
                  initiator_type="preflight"),
            entry(HERO, 800, 950, page=pid),
        ]

    runs = three_runs(tmp_path, build)

    assert [r.index for r in runs] == [1, 2, 3]
    assert [len(r.requests) for r in runs] == [2, 2, 2]
    assert runs[0].request_for(HERO).end_ms == 950


def test_a_playwright_har_without_wpt_fields_still_loads(tmp_path):
    har = {"log": {"pages": [{"id": "p", "startedDateTime": T0.isoformat(), "title": ""}],
                   "entries": [{"pageref": "p", "startedDateTime": (T0 + timedelta(milliseconds=50)).isoformat(),
                                "time": 100, "request": {"url": HERO, "method": "GET"},
                                "response": {"status": 200, "content": {"mimeType": "image/jpeg"}}}]}}
    path = tmp_path / "pw.har"
    path.write_text(json.dumps(har), encoding="utf-8")

    (run,) = hc.load_har(path)

    assert run.requests[0].start_ms == pytest.approx(50)
    assert run.requests[0].end_ms == pytest.approx(150)
    assert run.lcp_ms is None


# --------------------------------------------------------------------------- #
# OAK-39155 / OAK-39157: the image waits to be painted
# --------------------------------------------------------------------------- #
def _hero_waits_for_script(pid):
    return page(pid, lcp=2034, lcp_url=HERO), [
        entry(HERO, 775, 948, page=pid),
        entry("https://www.oakley.com/_ui/newHeroBanner.js", 1678, 1763, page=pid,
              ctype="application/javascript", initiator_type="script"),
    ]


def test_an_image_painted_only_after_a_script_is_confirmed(tmp_path):
    runs = three_runs(tmp_path, _hero_waits_for_script)

    result = hc.lcp_render_delay(runs, script="newHeroBanner.js")

    assert result.status == hc.CONFIRMED
    assert (result.runs_matched, result.runs_total) == (3, 3)
    assert "1086 ms" in result.summary
    assert "newHeroBanner.js finished at 1763 ms" in result.evidence[0]


def test_a_prompt_paint_is_not_a_delay(tmp_path):
    def build(pid):
        return page(pid, lcp=1000, lcp_url=HERO), [entry(HERO, 700, 950, page=pid)]

    result = hc.lcp_render_delay(three_runs(tmp_path, build), min_gap_ms=250)

    assert result.status == hc.NOT_SEEN


def test_a_delay_not_explained_by_the_named_script_is_not_that_ticket(tmp_path):
    """The gap alone does not prove "hidden until newHeroBanner.js runs"."""
    def build(pid):
        return page(pid, lcp=2034, lcp_url=HERO), [
            entry(HERO, 775, 948, page=pid),
            entry("https://www.oakley.com/_ui/newHeroBanner.js", 2100, 2200, page=pid,
                  ctype="application/javascript"),
        ]

    result = hc.lcp_render_delay(three_runs(tmp_path, build), script="newHeroBanner.js")

    assert result.status == hc.NOT_SEEN


def test_no_lcp_url_means_no_data_not_a_verdict(tmp_path):
    def build(pid):
        return page(pid), [entry(HERO, 700, 950, page=pid)]

    assert hc.lcp_render_delay(three_runs(tmp_path, build)).status == hc.NO_DATA


# --------------------------------------------------------------------------- #
# OAK-39154 / OAK-38965: preload and <picture> disagree
# --------------------------------------------------------------------------- #
def test_a_preload_at_the_wrong_width_is_confirmed(tmp_path):
    def build(pid):
        return page(pid, lcp=2107, lcp_url=PDP_IMG.format(w=2000), console=[
            {"level": "warning", "text": "The resource x was preloaded using link preload "
                                         "but not used within a few seconds"}]), [
            entry(PDP_IMG.format(w=1000), 1264, 1400, page=pid),
            entry(PDP_IMG.format(w=2000), 1469, 1769, page=pid, priority="Low"),
        ]

    result = hc.preload_mismatch(three_runs(tmp_path, build))

    assert result.status == hc.CONFIRMED
    assert "width=1000" in result.evidence[0] and "width=2000" in result.evidence[0]
    assert "preload not used" in result.evidence[0]


def test_a_preload_that_matches_is_not_a_mismatch(tmp_path):
    def build(pid):
        return page(pid, lcp=1861, lcp_url=PDP_IMG.format(w=768)), [
            entry(PDP_IMG.format(w=768), 872, 960, page=pid),
            entry(PDP_IMG.format(w=180), 1165, 1255, page=pid, priority="Low"),
        ]

    assert hc.preload_mismatch(three_runs(tmp_path, build)).status == hc.NOT_SEEN


# --------------------------------------------------------------------------- #
# OAK-37050: duplicate downloads, and renditions
# --------------------------------------------------------------------------- #
def test_the_same_url_downloaded_twice_is_confirmed(tmp_path):
    fr = "https://assets2.oakley.com/p/0OO9102__fr.png?width=2000"

    def build(pid):
        return page(pid), [entry(fr, 3685, 3926, page=pid), entry(fr, 3916, 3956, page=pid)]

    result = hc.duplicate_downloads(three_runs(tmp_path, build))

    assert result.status == hc.CONFIRMED
    assert "×2 at 3685 ms, 3916 ms" in result.evidence[0]


def test_one_image_at_two_widths_is_a_rendition_not_a_duplicate(tmp_path):
    def build(pid):
        return page(pid), [entry(PDP_IMG.format(w=1000), 100, 200, page=pid),
                           entry(PDP_IMG.format(w=3000), 300, 600, page=pid)]

    runs = three_runs(tmp_path, build)

    assert hc.duplicate_downloads(runs).status == hc.NOT_SEEN
    renditions = hc.multiple_renditions(runs)
    assert renditions.status == hc.CONFIRMED
    assert "1000, 3000" in renditions.evidence[0]


# --------------------------------------------------------------------------- #
# OAK-38307: a slide-in animation shifting the layout
# --------------------------------------------------------------------------- #
def test_a_frame_by_frame_slide_in_is_confirmed(tmp_path):
    steps = [(6632, 304, 0.0082), (6719, 213, 0.0182), (6821, 143, 0.0196),
             (6919, 93, 0.0168), (7018, 59, 0.0127), (7117, 36, 0.0092)]
    shifts = [{"time": t, "score": s, "rects": [[x, 94, 390 - x, 194]]} for t, x, s in steps]

    def build(pid):
        return page(pid, cls=0.095, shifts=shifts), []

    result = hc.slide_in_shift(three_runs(tmp_path, build))

    assert result.status == hc.CONFIRMED
    assert "6 shifts of the band y=94–288px between 6632 and 7117 ms" in result.evidence[0]


def test_a_single_shift_is_not_a_slide_in(tmp_path):
    def build(pid):
        return page(pid, cls=0.055, shifts=[
            {"time": 5198, "score": 0.055, "rects": [[0, 317, 390, 346]]}]), []

    assert hc.slide_in_shift(three_runs(tmp_path, build)).status == hc.NOT_SEEN


# --------------------------------------------------------------------------- #
# OAK-36852, OAK-38963, OAK-38968
# --------------------------------------------------------------------------- #
def test_a_missing_service_worker_is_confirmed_and_a_working_one_is_not(tmp_path):
    def broken(pid):
        return page(pid), [entry("https://www.oakley.com/service-worker.js", 0, 50,
                                 page=pid, status=404, ctype="text/html")]

    def working(pid):
        return page(pid), [entry("https://www.oakley.com/en-us/service-worker.js", 0, 50,
                                 page=pid, ctype="application/javascript")]

    assert hc.service_worker(three_runs(tmp_path, broken)).status == hc.CONFIRMED
    assert hc.service_worker(three_runs(tmp_path, working)).status == hc.NOT_SEEN


def test_a_script_initiated_hero_is_confirmed(tmp_path):
    def build(pid):
        return page(pid, lcp=2500, lcp_url=HERO), [
            entry(HERO, 1700, 1900, page=pid, initiator_type="script",
                  initiator="https://www.oakley.com/lazy-load-images.js", priority="Low")]

    result = hc.lcp_image_from_script(three_runs(tmp_path, build))

    assert result.status == hc.CONFIRMED
    assert "lazy-load-images.js" in result.evidence[0]


def test_a_parser_discovered_hero_is_not_lazy_loaded(tmp_path):
    runs = three_runs(tmp_path, _hero_waits_for_script)

    assert hc.lcp_image_from_script(runs).status == hc.NOT_SEEN


def test_images_requested_by_a_stylesheet_are_css_backgrounds(tmp_path):
    def build(pid):
        return page(pid), [
            entry(f"https://assets2.oakley.com/tile{i}.png", 100, 200, page=pid,
                  ctype="image/png", initiator_type="other",
                  initiator="https://www.oakley.com/plp.css?v=1")
            for i in range(4)]

    assert hc.css_background_images(three_runs(tmp_path, build)).status == hc.CONFIRMED


def test_css_icons_do_not_make_product_tiles_css_backgrounds(tmp_path):
    """The live OO PLP: menu icons and a spinner come from CSS, the product
    tiles are <img> - which is not what OAK-38968 describes."""
    def build(pid):
        icons = [entry(f"https://www.oakley.com/icons/i{i}.svg", 100, 150, page=pid,
                       ctype="image/svg+xml", initiator="https://www.oakley.com/main.css")
                 for i in range(8)]
        tiles = [entry(f"https://assets2.oakley.com/pieyewear/t{i}.png", 300, 400, page=pid,
                       ctype="image/png") for i in range(6)]
        return page(pid), icons + tiles

    runs = three_runs(tmp_path, build)

    assert hc.css_background_images(runs).status == hc.NOT_SEEN
    assert hc.css_background_images(runs, match="pieyewear").status == hc.NOT_SEEN


# --------------------------------------------------------------------------- #
# what the test itself caused, and the untracked list
# --------------------------------------------------------------------------- #
def test_cors_failures_caused_by_the_test_header_are_attributed_to_the_test(tmp_path):
    console = [{"level": "error", "category": "cors", "text":
                "Access to XMLHttpRequest at 'https://cdn.cookielaw.org/consent/x.json' from "
                "origin 'https://www.oakley.com' has been blocked by CORS policy: Request "
                "header field x-akamai-bot is not allowed by Access-Control-Allow-Headers"}]

    def build(pid):
        return page(pid, console=console), []

    runs = three_runs(tmp_path, build)

    assert hc.header_artifacts(runs, ["X-Akamai-Bot"]) == ["cdn.cookielaw.org"]
    assert hc.header_artifacts(runs, ["X-Other"]) == []
    assert hc.header_artifacts(runs) == ["cdn.cookielaw.org"]
    assert hc.rejected_headers(runs) == ["x-akamai-bot"]


def test_untracked_findings_list_render_blocking_and_large_shifts(tmp_path):
    def build(pid):
        return page(pid, lcp=2034, lcp_url=HERO, shifts=[
            {"time": 5181, "score": 0.0551, "rects": [[0, 317, 390, 346]]}]), [
            entry("https://www.oakley.com/en-us", 0, 400, page=pid, ctype="text/html"),
            entry("https://www.oakley.com/main.min.css", 441, 611, page=pid,
                  ctype="text/css", render_blocking="blocking"),
            entry(HERO, 775, 948, page=pid),
        ]

    findings = hc.untracked_findings(three_runs(tmp_path, build))

    header = findings.index(next(f for f in findings if "render-blocking" in f))
    assert findings[header + 1].strip() == "https://www.oakley.com/main.min.css 170 ms"
    assert any("Layout shift 0.055 at 5181 ms" in f and "y=317" in f for f in findings)
    assert any(f.startswith("LCP 2034 ms across 3 runs") for f in findings)


def test_images_competing_with_the_lcp_image_at_high_priority_are_listed(tmp_path):
    """The live OO homepage fetches a placeholder "empty" hero at High priority
    alongside the real one - bandwidth the LCP image does not get."""
    empty = "https://media.oakley.com/cms/1879572/portrait_ratio375x600/750/ww-empty-l1-hero-m.png"

    def build(pid):
        return page(pid, lcp=2034, lcp_url=HERO), [
            entry(HERO, 775, 948, page=pid),
            entry(empty, 794, 951, page=pid, ctype="image/png"),
            entry("https://media.oakley.com/tile.jpg", 1840, 1963, page=pid, priority="Low"),
        ]

    findings = hc.untracked_findings(three_runs(tmp_path, build))

    header = findings.index(next(f for f in findings if "compete" in f))
    assert findings[header].startswith("1 other image(s) compete")
    assert "ww-empty-l1-hero-m.png 794–951 ms" in findings[header + 1]
    assert not any("tile.jpg" in f for f in findings)


def test_evidence_urls_are_redacted(tmp_path):
    secret = "https://assets2.oakley.com/p.png?width=2000&token=SECRET123"

    def build(pid):
        return page(pid), [entry(secret, 100, 200, page=pid), entry(secret, 300, 400, page=pid)]

    evidence = " ".join(hc.duplicate_downloads(three_runs(tmp_path, build)).evidence)

    assert "SECRET123" not in evidence
