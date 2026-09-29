"""Unit tests for analysis/discovery.py.

Synthetic HARs in the shape of the WebPageTest exports the rules were written
against (real ones carry cookies). Each rule is shown finding its problem when
most runs have it, and staying quiet when only one does or the data is absent.
"""
from __future__ import annotations

import pytest

from analysis import discovery as dv
from analysis import har_checks as hc
from analysis.tickets import Capture, TicketCatalog, TicketSpec, load_catalog
from tests.unit.har_checks_test import entry, page, three_runs

SITE = "https://www.oakley.com"


def wpt_entry(url, *, cpu=0, cache=None, encoding="br", status=200, ctype="text/css",
              request_type="Stylesheet", bytes_in=5000, blocking=None, redirect=None,
              pid="page_1", start=100, end=200, priority="High"):
    e = entry(url, start, end, page=pid, ctype=ctype, status=status,
              request_type=request_type, render_blocking=blocking, priority=priority)
    e.update({"_cpuTime": cpu, "_bytesIn": bytes_in, "_contentEncoding": encoding,
              "_cacheControl": cache or "public, max-age=31536000, immutable"})
    if redirect:
        e["response"]["redirectURL"] = redirect
    return e


def wpt_page(pid, **extra):
    p = page(pid)
    p.update(extra)
    return p


def runs_where(tmp_path, build, hit_runs=(1, 2, 3)):
    """Three runs; ``build(pid, hit)`` gives each run's (page, entries)."""
    def per(pid):
        return build(pid, int(pid.rsplit("_", 1)[1]) in hit_runs)
    return three_runs(tmp_path, per)


def document(pid, cpu=0):
    return wpt_entry(f"{SITE}/en-us", pid=pid, ctype="text/html", request_type="Document",
                     start=0, end=100, cache="no-store", cpu=cpu)


def script(url, pid, cpu):
    return wpt_entry(url, pid=pid, cpu=cpu, ctype="application/javascript",
                     request_type="Script")


# --------------------------------------------------------------------------- #
# majority
# --------------------------------------------------------------------------- #
def test_a_problem_in_one_run_of_three_is_noise(tmp_path):
    def build(pid, hit):
        return wpt_page(pid), [document(pid),
                               script(f"{SITE}/_ui/require.js?v=1", pid, 900 if hit else 10)]

    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    assert dv.main_thread_scripts(runs_where(tmp_path / "one", build, hit_runs=(2,))) is None
    assert dv.main_thread_scripts(runs_where(tmp_path / "two", build, hit_runs=(1, 2)))


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def test_main_thread_scripts_names_the_script_and_the_tbt(tmp_path):
    def build(pid, _hit):
        return wpt_page(pid, _TotalBlockingTime=654, _longTasks=[[1000, 1340]]), [
            document(pid, cpu=172),
            script(f"{SITE}/_ui/require.js?v=2026-09-16", pid, 963),
            script(f"{SITE}/_ui/small.js", pid, 20),
            wpt_entry(f"{SITE}/_ui/huge.css", pid=pid, cpu=400)]

    found = dv.main_thread_scripts(runs_where(tmp_path, build))

    assert found.severity == "high"
    assert found.headline == "require.js runs 963 ms of main-thread script during load"
    assert not any("small.js" in line or "huge.css" in line for line in found.evidence)
    assert "172 ms median — the HTML document (parsing and inline scripts)" in found.evidence
    assert "Total Blocking Time 654 ms; longest task 340 ms across 3 runs" in found.evidence


def test_a_playwright_har_without_cpu_attribution_finds_nothing(tmp_path):
    def build(pid, _hit):
        return wpt_page(pid), [document(pid)]

    assert dv.main_thread_scripts(runs_where(tmp_path, build)) is None


def test_third_party_cost_groups_a_vendors_hosts(tmp_path):
    def build(pid, _hit):
        return wpt_page(pid), [
            document(pid),
            wpt_entry("https://cdn0.forter.com/a.js", pid=pid, cpu=60, bytes_in=200_000,
                      request_type="Script", ctype="application/javascript"),
            wpt_entry("https://5944d031151c.cdn4.forter.com/b.js", pid=pid, cpu=60,
                      bytes_in=30_000, request_type="Script", ctype="application/javascript"),
            wpt_entry("https://media.oakley.com/hero.jpg", pid=pid, bytes_in=900_000,
                      ctype="image/jpeg", request_type="Image")]

    found = dv.third_party_cost(runs_where(tmp_path, build))

    assert found.evidence[0].startswith("forter.com: 2 requests, 225 KB, 120 ms CPU")
    assert not any("oakley.com" in line for line in found.evidence)


def test_render_blocking_only_counts_other_origins(tmp_path):
    def build(pid, _hit):
        return wpt_page(pid), [
            document(pid),
            wpt_entry("https://cdn.jsdelivr.net/npm/swiper@11/swiper-bundle.min.css",
                      pid=pid, blocking="blocking"),
            wpt_entry(f"{SITE}/_ui/main.min.css", pid=pid, blocking="blocking")]

    found = dv.third_party_render_blocking(runs_where(tmp_path, build))

    assert found.headline == "First paint waits for third-party swiper-bundle.min.css"
    assert len(found.evidence) == 1


def test_redirects_and_two_react_majors(tmp_path):
    def build(pid, _hit):
        return wpt_page(pid), [
            document(pid),
            wpt_entry("https://unpkg.com/react@17/umd/react.production.min.js", pid=pid,
                      status=302, redirect="/react@17.0.2/umd/react.production.min.js"),
            wpt_entry("https://unpkg.com/react@17.0.2/umd/react.production.min.js", pid=pid),
            wpt_entry("https://unpkg.com/react@16.14.0/umd/react.production.min.js", pid=pid)]

    runs = runs_where(tmp_path, build)

    redirects = dv.redirected_subresources(runs)
    assert ("302 https://unpkg.com/react@17/umd/react.production.min.js → "
            "/react@17.0.2/umd/react.production.min.js") in redirects.evidence[0]
    assert dv.duplicate_libraries(runs).headline == \
        "Two versions of the same library load: react 16 and 17"


def test_versioned_assets_cached_for_an_hour(tmp_path):
    def build(pid, _hit):
        return wpt_page(pid), [document(pid)] + [
            wpt_entry(f"{SITE}/_ui/s{i}.js?v=2026-09-16", pid=pid, cache="max-age=3600",
                      request_type="Script", ctype="application/javascript")
            for i in range(3)] + [
            wpt_entry("https://assets2.oakley.com/p.png", pid=pid, ctype="image/png",
                      request_type="Image", cache="private, no-transform, max-age=281269"),
            wpt_entry("https://cdn0.forter.com/x.js", pid=pid, cache="max-age=60",
                      request_type="Script", ctype="application/javascript")]

    found = dv.short_static_cache(runs_where(tmp_path, build))

    assert found.headline == ("4 static assets are cached for under a week "
                              "(3 with max-age 1 hour)")
    assert "3 of them carry a version in the URL" in found.summary
    assert any(line.startswith("private, no-transform, max-age 3 days on 1 asset")
               for line in found.evidence)
    assert not any("x.js" in line for line in found.evidence)   # third party


def test_uncompressed_text_and_small_files(tmp_path):
    def build(pid, _hit):
        return wpt_page(pid), [
            document(pid),
            wpt_entry(f"{SITE}/big.js", pid=pid, encoding="", bytes_in=80_000,
                      ctype="application/javascript"),
            wpt_entry(f"{SITE}/tiny.css", pid=pid, encoding="", bytes_in=900)]

    found = dv.uncompressed_text(runs_where(tmp_path, build))

    assert found.headline == "1 text response(s) are served uncompressed (78 KB)"


def test_font_display_auto(tmp_path):
    fonts = [{"family": "AvenirNext", "display": "auto", "status": "loaded", "weight": "700"},
             {"family": "Icons", "display": "swap", "status": "loaded", "weight": "400"},
             {"family": "Unused", "display": "block", "status": "unloaded"}]

    def build(pid, _hit):
        return wpt_page(pid, _fonts=fonts), [document(pid)]

    found = dv.font_display(runs_where(tmp_path, build))

    assert found.evidence == ["AvenirNext 700: font-display auto"]


def test_dom_size_thresholds(tmp_path):
    def build_for(size):
        def build(pid, _hit):
            return wpt_page(pid, _domElements=size), [document(pid)]
        return build

    for sub in ("a", "b", "c"):
        (tmp_path / sub).mkdir()
    assert dv.dom_size(runs_where(tmp_path / "a", build_for(1200))) is None
    assert dv.dom_size(runs_where(tmp_path / "b", build_for(2000))).severity == "medium"
    assert dv.dom_size(runs_where(tmp_path / "c", build_for(8915))).headline == \
        "The page builds 8,915 DOM elements"


def test_lcp_image_at_low_priority(tmp_path):
    hero = "https://assets2.oakley.com/p.png?width=2000"

    def build(pid, _hit):
        p = wpt_page(pid)
        p.update({"_chromeUserTiming.LargestContentfulPaint": 2100,
                  "_LargestContentfulPaintImageURL": hero})
        return p, [document(pid), wpt_entry(hero, pid=pid, ctype="image/png",
                                            request_type="Image", priority="Low")]

    found = dv.lcp_priority(runs_where(tmp_path, build))

    assert found.severity == "high"
    assert "priority Low" in found.evidence[0]


def test_failed_requests_leave_out_what_the_capture_broke(tmp_path):
    broken = "https://media.oakley.com/lense-view-module/main.js"

    def build(pid, _hit):
        preflight = entry(broken, 100, 150, page=pid, status=501, request_type="Preflight")
        preflight["request"]["method"] = "OPTIONS"
        tracker = wpt_entry("https://cdn0.forter.com/x.js", pid=pid)
        tracker["request"]["headers"] = [{"name": "x-akamai-bot", "value": "t"}]
        return wpt_page(pid), [
            document(pid), preflight, tracker,
            wpt_entry(broken, pid=pid, status=501),
            wpt_entry(f"{SITE}/missing.css", pid=pid, status=404)]

    found = dv.failed_requests(runs_where(tmp_path, build))

    assert found.evidence == ["404 https://www.oakley.com/missing.css"]


def test_console_errors_skip_network_cors_and_service_worker_lines(tmp_path):
    console = [
        {"level": "log", "source": "console-api",
         "text": "TypeError: Cannot read properties of undefined (reading 'loadFromElement')"},
        {"level": "error", "source": "network", "text": "Failed to load resource: net::ERR_FAILED"},
        {"level": "log", "text": "Service Worker registrazione fallita: TypeError: Failed to "
                                 "register a ServiceWorker for scope ('x'): evaluation failed"},
    ]

    def build(pid, _hit):
        return wpt_page(pid, _consoleLog=console), [document(pid)]

    found = dv.console_errors(runs_where(tmp_path, build))

    assert found.evidence == [
        "TypeError: Cannot read properties of undefined (reading 'loadFromElement')"]


def test_vulnerable_libraries_take_the_worst_advisory(tmp_path):
    vulns = [{"name": "jquery", "version": "1.11.2", "severity": "medium", "url": "u1"},
             {"name": "jquery-ui", "version": "1.10.2", "severity": "high", "url": "u2"}]

    def build(pid, _hit):
        return wpt_page(pid, _jsLibsVulns=vulns), [document(pid)]

    found = dv.vulnerable_libraries(runs_where(tmp_path, build))

    assert found.severity == "high"
    assert found.evidence[0].startswith("jquery-ui 1.10.2: 1 advisory(ies), worst high")


def test_oversized_images_account_for_the_pixel_ratio(tmp_path):
    images = [
        {"url": "https://assets2.oakley.com/big.png", "width": 439, "naturalWidth": 2000},
        {"url": "https://assets2.oakley.com/retina.png", "width": 300, "naturalWidth": 900},
        {"url": "https://www.oakley.com/icon.svg", "width": 35, "naturalWidth": 560},
    ]

    def build_at(dpr):
        def build(pid, _hit):
            return wpt_page(pid, _Images=images, _viewport={"dpr": dpr}), [document(pid)]
        return build

    (tmp_path / "x1").mkdir()
    (tmp_path / "x3").mkdir()
    at_1x = dv.oversized_images(runs_where(tmp_path / "x1", build_at(1)))
    at_3x = dv.oversized_images(runs_where(tmp_path / "x3", build_at(3)))

    assert len(at_1x.evidence) == 2
    assert at_3x.evidence == ["2000 px wide in a 439 px slot at 3x — "
                              "https://assets2.oakley.com/big.png"]


# --------------------------------------------------------------------------- #
# across captures
# --------------------------------------------------------------------------- #
def _capture(tmp_path, page_name, device, dom):
    def build(pid, _hit):
        return wpt_page(pid, _domElements=dom), [document(pid)]
    sub = tmp_path / f"{page_name}-{device}"
    sub.mkdir()
    return Capture(page=page_name, device=device, runs=runs_where(sub, build))


def test_one_proposal_per_rule_titled_from_the_worst_capture(tmp_path):
    captures = [_capture(tmp_path, "homepage", "mobile", 4854),
                _capture(tmp_path, "plp", "mobile", 8915),
                _capture(tmp_path, "pdp", "mobile", 1000)]

    [proposal] = [p for p in dv.discover(captures) if p.key == "dom_size"]

    assert proposal.title == "HP / PLP | The page builds 8,915 DOM elements"
    assert proposal.seen_on == ["homepage/mobile", "plp/mobile"]
    assert proposal.captures_total == 3
    assert proposal.severity == "high"


def test_all_pages_and_tracking_tickets(tmp_path):
    captures = [_capture(tmp_path, "homepage", "mobile", 4854),
                _capture(tmp_path, "plp", "mobile", 8915)]
    catalog = TicketCatalog(tickets=[
        TicketSpec(id="OAK-1", title="DOM", check=None, reason="r", tracks=["dom_size"])])

    [proposal] = [p for p in dv.discover(captures, catalog) if p.key == "dom_size"]

    assert proposal.title.startswith("All pages | ")
    assert proposal.tracked_by == "OAK-1"


def test_related_tickets_come_from_the_checks_they_share(tmp_path):
    hero = "https://assets2.oakley.com/p.png?width=2000"

    def build(pid, _hit):
        p = wpt_page(pid)
        p.update({"_chromeUserTiming.LargestContentfulPaint": 2100,
                  "_LargestContentfulPaintImageURL": hero})
        return p, [document(pid), wpt_entry(hero, pid=pid, priority="Low",
                                            ctype="image/png", request_type="Image")]

    captures = [Capture(page="pdp", device="desktop", runs=runs_where(tmp_path, build))]
    catalog = TicketCatalog(tickets=[
        TicketSpec(id="OAK-39154", title="t", check="preload_mismatch"),
        TicketSpec(id="OAK-37050", title="t", check="duplicate_downloads")])

    [proposal] = [p for p in dv.discover(captures, catalog) if p.key == "lcp_priority"]

    assert proposal.related == ["OAK-39154"]


def test_proposals_are_ordered_most_severe_first(tmp_path):
    captures = [_capture(tmp_path, "homepage", "mobile", 2000)]
    for run in captures[0].runs:
        run.fonts = [{"family": "A", "display": "auto", "status": "loaded"}]
        run.dom_elements = 9000

    severities = [p.severity for p in dv.discover(captures)]
    assert severities == sorted(severities, key=dv.SEVERITIES.index)


def test_the_catalog_rejects_an_unknown_rule(tmp_path):
    path = tmp_path / "tickets.yaml"
    path.write_text("tickets:\n  - {id: X, title: t, reason: r, tracks: [dom_sise]}\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="dom_sise"):
        load_catalog(path)


def test_every_rule_has_a_fix_and_a_test_for_done():
    for rule in dv.RULES:
        assert rule.fix and rule.done_when, rule.id
        for check in rule.related_checks:
            assert check in hc.CHECKS, (rule.id, check)
