"""End-to-end browser ingestion against a real site (TESTING_PLAN.md §3).

Runs a real headless Chromium campaign over one page x (mid-mobile, desktop)
and asserts a *complete* normalized run JSON comes out the other side — the
canonical schema is the gate, so a missing CWV metric fails the test.

Marked ``e2e``: excluded from the offline unit gate (``pytest -m "not e2e"``)
because it needs Playwright Chromium and public network access. Run with::

    python -m playwright install chromium
    pytest -m e2e -v

The Lighthouse Node bridge is out of scope here (see ingest/browser/lighthouse.py);
a stub supplies empty category scores, which the schema accepts as optional.
"""
from __future__ import annotations

import json

import pytest

from config.load import Device, Network, PageTarget, PageTest, ProjectConfig, Settings
from ingest import automated
from normalize.schema import Run

pytestmark = pytest.mark.e2e

# A small, stable, public page. Kept deliberately simple so the assertions are
# about our pipeline, not about a third party's page weight.
E2E_URL = "https://example.com/"

MID_MOBILE = Device(
    name="mid-mobile", label="Mid-level Android phone", viewport_width=393,
    viewport_height=851, device_scale_factor=2.75, mobile=True, cpu_throttle=4,
)
DESKTOP = Device(
    name="desktop", label="Desktop (Full HD)", viewport_width=1350,
    viewport_height=940, device_scale_factor=1, mobile=False, cpu_throttle=1,
)
SLOW_4G = Network(name="slow-4g", latency_ms=170, downlink_mbps=4.0, uplink_mbps=3.0)
FAST_3G = Network(name="fast-3g", latency_ms=150, downlink_mbps=1.6, uplink_mbps=0.75)


def _no_lighthouse(url, cdp):
    """Stub Lighthouse report — the Node bridge is not wired in CI."""
    return {"categories": {}}


@pytest.fixture(scope="module")
def browser():
    """A real headless Chromium; skips cleanly when Playwright is unavailable."""
    playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed"
    )
    try:
        pw = playwright.sync_playwright().start()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright could not start: {exc}")
    try:
        instance = pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover - browser not installed
        pw.stop()
        pytest.skip(f"Chromium unavailable (run `playwright install chromium`): {exc}")
    yield instance
    instance.close()
    pw.stop()


@pytest.fixture(scope="module")
def e2e_config() -> ProjectConfig:
    """One page x two conditions (mobile + desktop), a single run each."""
    return ProjectConfig(
        settings=Settings(),
        devices={"mid-mobile": MID_MOBILE, "desktop": DESKTOP},
        networks={"slow-4g": SLOW_4G, "fast-3g": FAST_3G},
        project="e2e-smoke",
        pages=[
            PageTarget(name="homepage", url=E2E_URL, tests=[
                PageTest(device="mid-mobile", network="slow-4g", runs=1),
                PageTest(device="desktop", network="fast-3g", runs=1),
            ])
        ],
    )


# A page with the shape of a real app: stylesheet, image, a late layout shift,
# a long task during load, and interaction handlers that do real work. Served
# over the real https URL so the SSRF gate stays fully in force, while keeping
# the measurements deterministic instead of at a third party's mercy.
REALISTIC_PAGE = """
<html><head><style>
  body { font-family: sans-serif; margin: 0; }
  .hero { height: 320px; background: linear-gradient(#123, #567); color: #fff; }
  .banner { height: 90px; background: #eee; }
</style></head>
<body>
  <div class="hero"><h1>Storefront</h1><p>Product of the week</p></div>
  <div id="late"></div>
  <p>Body copy that makes the document non-trivial for LCP candidacy.</p>
  <script>
    // Long task during load -> non-zero Total Blocking Time. Fixed *work*
    // rather than a wall-clock loop, so its cost scales with CPU throttling.
    (() => { let x = 0; for (let i = 0; i < 4e6; i++) { x += Math.sqrt(i); } window.__W = x; })();
    // Late DOM insertion -> a real layout shift (CLS).
    setTimeout(() => {
      const el = document.createElement('div');
      el.className = 'banner';
      el.textContent = 'Injected banner';
      document.getElementById('late').appendChild(el);
    }, 120);
    // Interaction handlers that block ~120ms -> a real, non-floor INP.
    const block = () => { const s = performance.now(); while (performance.now() - s < 120) {} };
    document.addEventListener('keydown', block);
    document.addEventListener('click', block);
  </script>
</body></html>
"""


def serve_realistic_page(page):
    """Fulfil every request with the realistic fixture above."""
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=REALISTIC_PAGE
        ),
    )


@pytest.fixture(scope="module")
def campaign_runs(browser, e2e_config, tmp_path_factory):
    """Run the real campaign once; every assertion below reads its output.

    Only genuine environment failures (browser/network) skip. A schema
    ValidationError is a real defect and must fail the suite, never be
    swallowed as "unreachable".
    """
    from ingest.browser.runner import BrowserRunner

    artifacts = tmp_path_factory.mktemp("e2e_artifacts")
    runner = BrowserRunner(
        browser, setup_page_fn=serve_realistic_page, run_lighthouse_fn=_no_lighthouse
    )
    return automated.run_campaign(
        e2e_config, runner, artifacts_root=str(artifacts)
    ), artifacts


def test_campaign_emits_one_run_per_condition(campaign_runs):
    runs, _ = campaign_runs
    assert len(runs) == 2
    assert {r.condition.device for r in runs} == {"mid-mobile", "desktop"}


def test_runs_are_valid_canonical_objects(campaign_runs):
    """Round-trip through the schema: JSON out must re-validate cleanly."""
    runs, _ = campaign_runs
    for run in runs:
        payload = json.loads(json.dumps(run.model_dump(mode="json")))
        assert Run.model_validate(payload).run_id == run.run_id


def test_core_web_vitals_are_measured(campaign_runs):
    """The whole point: real LCP/CLS/FCP/TTFB came back from a real browser."""
    runs, _ = campaign_runs
    for run in runs:
        cwp = run.metrics.cwp
        assert cwp.lcp_ms is not None and cwp.lcp_ms > 0, "LCP not captured"
        assert cwp.cls is not None, "CLS not captured"
        assert cwp.inp_ms is not None, "INP not captured (synthetic interaction failed)"
        assert cwp.fcp_ms is not None and cwp.fcp_ms > 0
        assert cwp.ttfb_ms is not None and cwp.ttfb_ms > 0
        assert cwp.tbt_ms is not None, "TBT not derived from long tasks"


def test_main_thread_metrics_come_from_cdp(campaign_runs):
    """DevTools counters over CDP — no Lighthouse Node bridge involved."""
    runs, _ = campaign_runs
    for run in runs:
        mt = run.metrics.main_thread
        assert mt.task_ms is not None and mt.task_ms >= 0
        assert mt.dom_nodes is not None and mt.dom_nodes > 0
        assert mt.js_heap_kb is not None and mt.js_heap_kb > 0


def test_device_emulation_reflected_in_condition(campaign_runs):
    runs, _ = campaign_runs
    by_device = {r.condition.device: r for r in runs}
    assert by_device["mid-mobile"].condition.cpu_throttle == 4
    assert by_device["desktop"].condition.cpu_throttle == 1
    assert by_device["mid-mobile"].condition.network == "slow-4g"


def test_artifacts_written_for_every_run(campaign_runs):
    runs, artifacts = campaign_runs
    for run in runs:
        captures = run.captures
        assert captures.screenshot and captures.har and captures.trace
        for path in (captures.screenshot, captures.har, captures.trace):
            from pathlib import Path
            assert Path(path).exists(), f"missing artifact: {path}"
            assert Path(path).stat().st_size > 0


def test_har_contains_no_response_bodies(campaign_runs):
    """SECURITY_PLAN.md §2.6 — recorded HAR must omit response content."""
    runs, _ = campaign_runs
    har = json.loads(open(runs[0].captures.har, encoding="utf-8").read())
    for entry in har["log"]["entries"]:
        content = entry.get("response", {}).get("content", {})
        assert "text" not in content, "HAR retained a response body"


def test_inp_reflects_real_interaction_latency(browser):
    """INP must be a real latency, not the Event Timing 16ms observer floor.

    ``example.com`` has no event handlers, so its interactions are faster than
    the API can resolve. Here the request is fulfilled with a page whose handler
    blocks ~120ms — the shape of a real app — over the same real https URL, so
    the SSRF gate stays fully in force.
    """
    from ingest.browser.runner import BrowserRunner

    slow_page = (
        "<html><body><h1>Interaction test</h1><p>content</p>"
        "<script>"
        "const block = () => { const s = performance.now();"
        " while (performance.now() - s < 120) {} };"
        "document.addEventListener('keydown', block);"
        "document.addEventListener('click', block);"
        "</script></body></html>"
    )

    def serve_fixture(page):
        page.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=slow_page
            ),
        )

    runner = BrowserRunner(
        browser, setup_page_fn=serve_fixture, run_lighthouse_fn=_no_lighthouse
    )
    result = runner.run_condition(E2E_URL, DESKTOP, FAST_3G)

    inp = result["cwp"]["inp_ms"]
    assert inp is not None, "no interaction entry produced"
    assert inp >= 100, f"INP {inp}ms looks like the 16ms observer floor, not real work"


def test_live_public_site_is_measurable(browser):
    """No fixture, no interception — a real request to a real public site.

    INP is deliberately not asserted: example.com registers no event handlers,
    so its interactions resolve faster than the Event Timing API's 16ms floor.
    That is a property of the page, not of the pipeline (see
    test_inp_reflects_real_interaction_latency).
    """
    from ingest.browser.runner import BrowserRunner

    runner = BrowserRunner(browser, run_lighthouse_fn=_no_lighthouse)
    try:
        result = runner.run_condition(E2E_URL, MID_MOBILE, SLOW_4G)
    except Exception as exc:  # pragma: no cover - offline / DNS-blocked CI
        pytest.skip(f"live site unreachable: {exc}")

    cwp = result["cwp"]
    assert cwp["lcp_ms"] and cwp["lcp_ms"] > 0
    assert cwp["fcp_ms"] and cwp["fcp_ms"] > 0
    assert cwp["ttfb_ms"] and cwp["ttfb_ms"] > 0
    assert cwp["cls"] is not None
    assert result["main_thread"]["dom_nodes"] > 0


def test_cpu_throttling_is_actually_applied(campaign_runs):
    """Proof the CDP emulation lands, rather than being silently dropped.

    The fixture burns a fixed amount of CPU work during load, so mid-mobile
    (4x throttle) must spend materially more main-thread time on it than
    desktop (1x). Asserted on CPU, not network: intercepted requests are
    fulfilled in-process and never traverse the throttled network stack.
    """
    runs, _ = campaign_runs
    by_device = {r.condition.device: r for r in runs}
    mobile_task_ms = by_device["mid-mobile"].metrics.main_thread.task_ms
    desktop_task_ms = by_device["desktop"].metrics.main_thread.task_ms
    assert mobile_task_ms > desktop_task_ms * 1.5, (
        f"mid-mobile is configured for 4x CPU throttle but spent "
        f"{mobile_task_ms}ms vs desktop's {desktop_task_ms}ms — "
        "CPU emulation looks inactive"
    )
