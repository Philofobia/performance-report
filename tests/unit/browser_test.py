"""Unit tests for ingest/browser/* and ingest/automated.py.

Playwright is fully mocked (fake browser/context/page/cdp); no real browser is
launched, and no network is touched (url_safety._lookup is monkeypatched where
DNS resolution would occur). Real-browser tests live in tests/e2e and are
marked @pytest.mark.e2e.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from config.load import Device, Network, PageTarget, PageTest, ProjectConfig, Settings
from ingest import automated
from ingest.browser import cdp_metrics, lighthouse, webser
from ingest.browser.runner import (
    BlockedResponseError,
    BrowserRunner,
    apply_cpu_throttle,
    apply_network_throttle,
    device_context_kwargs,
    network_throttle_params,
)
from normalize import url_safety


# --------------------------------------------------------------------------- #
# Fakes (mock the whole Playwright surface)
# --------------------------------------------------------------------------- #
class FakeCDP:
    def __init__(self, log=None):
        self.sent = []
        self._log = log

    def send(self, method, params=None):
        self.sent.append((method, params))
        if self._log is not None:
            self._log.append(f"cdp:{method}")


class FakeTracing:
    def __init__(self, log=None):
        self.starts = []
        self.stops = []
        self._log = log

    def start(self, **kwargs):
        self.starts.append(kwargs)
        if self._log is not None:
            self._log.append("tracing.start")

    def stop(self, **kwargs):
        self.stops.append(kwargs)
        if self._log is not None:
            self._log.append("tracing.stop")


class FakeKeyboard:
    def __init__(self, log):
        self.presses = []
        self._log = log

    def press(self, key):
        self.presses.append(key)
        self._log.append(f"key:{key}")


class FakeMouse:
    def __init__(self, log):
        self.clicks = []
        self._log = log

    def click(self, x, y):
        self.clicks.append((x, y))
        self._log.append("click")


class FakeResponse:
    """Minimal stand-in for a Playwright Response."""

    def __init__(self, status):
        self.status = status


class FakePage:
    """Fake Playwright page recording an ordered event log.

    ``main_status`` is the status of the document response returned by ``goto``;
    ``sub_statuses`` are sub-resource responses replayed to any ``response``
    listener when navigation happens.
    """

    def __init__(
        self,
        log,
        cwp=None,
        entries=None,
        goto_error=None,
        main_status=200,
        sub_statuses=(),
    ):
        self.log = log
        self.gotos = []
        self.screenshots = []
        self.wait_states = []
        self.init_scripts = []
        self.waited_functions = []
        self.listeners = {}
        self.keyboard = FakeKeyboard(log)
        self.mouse = FakeMouse(log)
        self._cwp = cwp if cwp is not None else {
            "lcp_ms": 100, "cls": 0.1, "inp_ms": 50, "fcp_ms": 60, "ttfb_ms": 20,
        }
        self._entries = entries or []
        self._goto_error = goto_error
        self._main_status = main_status
        self._sub_statuses = list(sub_statuses)

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)
        self.log.append(f"on:{event}")

    def add_init_script(self, script):
        self.init_scripts.append(script)
        self.log.append("add_init_script")

    def goto(self, url, **kwargs):
        self.log.append("goto")
        if self._goto_error:
            raise self._goto_error
        self.gotos.append((url, kwargs))
        for status in self._sub_statuses:
            for handler in self.listeners.get("response", []):
                handler(FakeResponse(status))
        return FakeResponse(self._main_status)

    def wait_for_load_state(self, *args, **kwargs):
        self.log.append(f"wait_for_load_state:{args[0] if args else ''}")
        self.wait_states.append((args, kwargs))

    def wait_for_function(self, expression, **kwargs):
        self.log.append("wait_for_function")
        self.waited_functions.append((expression, kwargs))

    def screenshot(self, **kwargs):
        self.log.append("screenshot")
        self.screenshots.append(kwargs)

    def evaluate(self, script, arg=None):
        if "getEntriesByType('resource')" in script:
            return self._entries
        if "elementFromPoint" in script:
            return {"x": 10, "y": 20}
        return self._cwp  # __PERF_CAPTURE__ read


class FakeTimeout(Exception):
    """Stands in for playwright's TimeoutError — the offline suite imports no
    browser package, and the runner catches by behaviour, not by type."""


class FakeContext:
    def __init__(self, log, page_kwargs=None, page_cls=None):
        self.log = log
        self.pages = []
        self.closed = False
        self.tracing = FakeTracing(log)
        self.cdp = FakeCDP(log)
        self._page_kwargs = page_kwargs or {}
        self._page_cls = page_cls or FakePage

    def new_page(self):
        page = self._page_cls(self.log, **self._page_kwargs)
        self.pages.append(page)
        return page

    def new_cdp_session(self, page):
        return self.cdp

    def close(self):
        self.closed = True
        self.log.append("context.close")


class FakeBrowser:
    def __init__(self, page_kwargs=None, page_cls=None):
        self.contexts = []
        self.log = []
        self._page_kwargs = page_kwargs
        self._page_cls = page_cls

    def new_context(self, **kwargs):
        ctx = FakeContext(self.log, self._page_kwargs, self._page_cls)
        self.contexts.append((kwargs, ctx))
        return ctx


DEVICE = Device(
    name="mid-mobile", viewport_width=393, viewport_height=851,
    device_scale_factor=2.75, mobile=True, cpu_throttle=4,
)
DESKTOP = Device(
    name="desktop", viewport_width=1350, viewport_height=940,
    device_scale_factor=1, mobile=False, cpu_throttle=1,
)
NETWORK = Network(name="slow-4g", latency_ms=170, downlink_mbps=4.0, uplink_mbps=3.0)
ONLINE = Network(name="online", latency_ms=0, downlink_mbps=None, uplink_mbps=None)
OFFLINE = Network(name="offline", latency_ms=0, downlink_mbps=0, uplink_mbps=0, offline=True)


def sample_collect(page):
    return {
        "cwp": {"lcp_ms": 6200, "cls": 0.42, "inp_ms": 480, "fcp_ms": 3100, "ttfb_ms": 1800},
        "network": {"total_transfer_kb": 4820.0, "request_count": 118, "render_blocking_css": 6},
        "resource_timings": [
            {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140.0, "duration_ms": 390.0}
        ],
    }


@pytest.fixture
def public_dns(monkeypatch):
    """Force DNS resolution to a public IP so validate_url(resolve=True) passes."""
    monkeypatch.setattr(url_safety, "_lookup", lambda host: {"8.8.8.8"})


def make_runner(browser, collect=None, lh_cdp=None, **kwargs):
    def collect_fn(page):
        return sample_collect(page) if collect is None else collect(page)

    def lh_fn(url, cdp):
        return {"performance": 80, "seo": 90} if lh_cdp is None else lh_cdp(url, cdp)

    return BrowserRunner(
        browser,
        collect_metrics_fn=collect_fn,
        run_lighthouse_fn=lh_fn,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Device + network preset resolution
# --------------------------------------------------------------------------- #
def test_device_context_kwargs_resolves_viewport_and_mobile_flags():
    kwargs = device_context_kwargs(DEVICE)
    assert kwargs["viewport"] == {"width": 393, "height": 851}
    assert kwargs["device_scale_factor"] == 2.75
    assert kwargs["is_mobile"] is True
    assert kwargs["has_touch"] is True


def test_device_context_kwargs_omits_absent_user_agent():
    assert "user_agent" not in device_context_kwargs(DEVICE)


def test_device_context_kwargs_includes_user_agent_when_set():
    device = DEVICE.model_copy(update={"user_agent": "TestAgent/1.0"})
    assert device_context_kwargs(device)["user_agent"] == "TestAgent/1.0"


def test_desktop_device_is_not_mobile():
    kwargs = device_context_kwargs(DESKTOP)
    assert kwargs["is_mobile"] is False and kwargs["has_touch"] is False


def test_network_throttle_params_converts_mbps_to_bytes_per_second():
    params = network_throttle_params(NETWORK)
    # 4 Mb/s -> 4_000_000 / 8 = 500_000 bytes/sec
    assert params["downloadThroughput"] == 500_000
    assert params["uploadThroughput"] == 375_000
    assert params["latency"] == 170
    assert params["offline"] is False


def test_network_throttle_params_unthrottled_uses_negative_one():
    params = network_throttle_params(ONLINE)
    assert params["downloadThroughput"] == -1
    assert params["uploadThroughput"] == -1


def test_network_throttle_params_offline_flag():
    assert network_throttle_params(OFFLINE)["offline"] is True


def test_apply_throttles_send_expected_cdp_commands():
    cdp = FakeCDP()
    apply_network_throttle(cdp, NETWORK)
    apply_cpu_throttle(cdp, DEVICE)
    methods = [m for m, _ in cdp.sent]
    assert methods == ["Network.emulateNetworkConditions", "Emulation.setCPUThrottlingRate"]
    assert cdp.sent[1][1] == {"rate": 4}


# --------------------------------------------------------------------------- #
# BrowserRunner.run_condition
# --------------------------------------------------------------------------- #
def test_run_condition_returns_full_measurement_shape(public_dns):
    browser = FakeBrowser()
    result = make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    assert set(result) == {
        "cwp", "main_thread", "network", "resource_timings", "lighthouse",
        "captures", "guard",
    }
    assert result["cwp"]["lcp_ms"] == 6200
    assert result["lighthouse"]["performance"] == 80


def test_run_condition_applies_device_emulation_to_context(public_dns):
    browser = FakeBrowser()
    make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    ctx_kwargs, _ = browser.contexts[0]
    assert ctx_kwargs["viewport"] == {"width": 393, "height": 851}
    assert ctx_kwargs["is_mobile"] is True


def test_run_condition_applies_cdp_throttling(public_dns):
    browser = FakeBrowser()
    make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    _, ctx = browser.contexts[0]
    sent = dict(ctx.cdp.sent)
    assert sent["Network.emulateNetworkConditions"]["latency"] == 170
    assert sent["Emulation.setCPUThrottlingRate"] == {"rate": 4}


def test_collector_is_installed_before_navigation(public_dns):
    """LCP/CLS/FCP are buffered entries — installing after goto loses them."""
    browser = FakeBrowser()
    make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    log = browser.log
    assert log.index("add_init_script") < log.index("goto")


def test_run_condition_waits_for_networkidle_when_online(public_dns):
    browser = FakeBrowser()
    make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    _, ctx = browser.contexts[0]
    assert any("networkidle" in str(a) for a, _ in ctx.pages[0].wait_states)


def test_run_condition_skips_networkidle_when_offline(public_dns):
    browser = FakeBrowser()
    make_runner(browser).run_condition("https://example.com/", DEVICE, OFFLINE)
    _, ctx = browser.contexts[0]
    assert ctx.pages[0].wait_states == []


def test_networkidle_timeout_does_not_abort_the_measurement(public_dns):
    """`load` already fired; networkidle is a settle, not a correctness gate.

    A commerce page with continuous beacons may never go idle. Aborting there
    discards the whole campaign — every condition already measured included —
    over a wait that was only ever an optimisation. Matches how the LCP/INP
    waits already treat a timeout: the measurement proceeds.
    """
    class NeverIdlePage(FakePage):
        def wait_for_load_state(self, *args, **kwargs):
            super().wait_for_load_state(*args, **kwargs)
            raise FakeTimeout("Timeout 5000ms exceeded.")

    browser = FakeBrowser(page_cls=NeverIdlePage)
    result = make_runner(browser).run_condition(
        "https://example.com/", DEVICE, NETWORK
    )
    # The run still yields real metrics rather than raising.
    assert result["cwp"]["lcp_ms"] == 6200
    _, ctx = browser.contexts[0]
    assert any("networkidle" in str(a) for a, _ in ctx.pages[0].wait_states)


def test_navigation_timeout_is_configurable(public_dns):
    """A heavy page under CPU+network throttling needs a larger budget."""
    browser = FakeBrowser()
    make_runner(browser, navigation_timeout_ms=90_000).run_condition(
        "https://example.com/", DEVICE, NETWORK
    )
    _, ctx = browser.contexts[0]
    _, kwargs = ctx.pages[0].gotos[0]
    assert kwargs["timeout"] == 90_000


def test_network_idle_timeout_is_configurable(public_dns):
    browser = FakeBrowser()
    make_runner(browser, network_idle_timeout_ms=20_000).run_condition(
        "https://example.com/", DEVICE, NETWORK
    )
    _, ctx = browser.contexts[0]
    _, kwargs = ctx.pages[0].wait_states[0]
    assert kwargs["timeout"] == 20_000


def test_navigation_timeout_still_propagates(public_dns):
    """Unlike networkidle, failing to load at all is a real failure."""
    browser = FakeBrowser(
        page_kwargs={"goto_error": FakeTimeout("Page.goto: Timeout 30000ms exceeded.")}
    )
    with pytest.raises(FakeTimeout):
        make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)


def test_interaction_is_triggered_after_lcp_settles(public_dns):
    """LCP freezes at first interaction, so it must settle first."""
    browser = FakeBrowser()
    make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    log = browser.log
    assert log.index("wait_for_function") < log.index("key:Escape")


def test_measure_inp_false_skips_synthetic_interaction(public_dns):
    browser = FakeBrowser()
    make_runner(browser, measure_inp=False).run_condition(
        "https://example.com/", DEVICE, NETWORK
    )
    assert "key:Escape" not in browser.log


def test_run_condition_without_artifacts_dir_captures_nothing(public_dns):
    browser = FakeBrowser()
    result = make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    assert result["captures"] == {"screenshot": None, "har": None, "trace": None}
    _, ctx = browser.contexts[0]
    assert ctx.tracing.starts == [] and ctx.pages[0].screenshots == []


def test_run_condition_writes_artifact_paths(public_dns, tmp_path):
    browser = FakeBrowser()
    out = tmp_path / "homepage" / "mid-mobile__slow-4g" / "run_1"
    result = make_runner(browser).run_condition(
        "https://example.com/", DEVICE, NETWORK,
        artifacts_dir=str(out), run_id="run_1",
    )
    captures = result["captures"]
    assert captures["har"].endswith("run_1.har")
    assert captures["trace"].endswith("run_1.trace.zip")
    assert captures["screenshot"].endswith("run_1.png")
    assert out.exists()  # directory created eagerly for HAR recording


def test_har_recording_omits_response_bodies(public_dns, tmp_path):
    """SECURITY_PLAN.md §2.6 — bodies can carry tokens/PII."""
    browser = FakeBrowser()
    make_runner(browser).run_condition(
        "https://example.com/", DEVICE, NETWORK, artifacts_dir=str(tmp_path)
    )
    ctx_kwargs, _ = browser.contexts[0]
    assert ctx_kwargs["record_har_content"] == "omit"


def test_tracing_started_and_stopped_with_artifacts(public_dns, tmp_path):
    browser = FakeBrowser()
    make_runner(browser).run_condition(
        "https://example.com/", DEVICE, NETWORK, artifacts_dir=str(tmp_path), run_id="r1"
    )
    _, ctx = browser.contexts[0]
    assert len(ctx.tracing.starts) == 1
    assert ctx.tracing.stops[0]["path"].endswith("r1.trace.zip")


def test_context_is_always_closed(public_dns):
    browser = FakeBrowser()
    make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    assert browser.contexts[0][1].closed is True


def test_context_is_closed_even_when_navigation_fails(public_dns):
    browser = FakeBrowser(page_kwargs={"goto_error": RuntimeError("nav boom")})
    with pytest.raises(RuntimeError, match="nav boom"):
        make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    assert browser.contexts[0][1].closed is True


# --------------------------------------------------------------------------- #
# SSRF gate (SECURITY_PLAN.md §2.2) — must reject BEFORE any navigation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",           # non-https
        "https://user:pass@example.com/",  # userinfo
        "https://192.168.1.10/",         # raw private IP
        "https://127.0.0.1/",            # loopback literal
        "ftp://example.com/",            # wrong scheme
        "https:///nohost",               # missing hostname
        "",                              # empty
    ],
)
def test_unsafe_urls_rejected_before_browser_is_touched(url):
    browser = FakeBrowser()
    with pytest.raises(url_safety.UnSafeURLError):
        make_runner(browser).run_condition(url, DEVICE, NETWORK)
    assert browser.contexts == []  # no context ever created


def test_host_resolving_to_private_ip_rejected(monkeypatch):
    monkeypatch.setattr(url_safety, "_lookup", lambda host: {"10.0.0.5"})
    browser = FakeBrowser()
    with pytest.raises(url_safety.UnSafeURLError, match="blocked/private"):
        make_runner(browser).run_condition("https://internal.example/", DEVICE, NETWORK)
    assert browser.contexts == []


# --------------------------------------------------------------------------- #
# webser pure helpers
# --------------------------------------------------------------------------- #
def test_compute_network_metrics_aggregates_transfer_and_counts():
    entries = [
        {"name": "https://x/a.js", "initiatorType": "script", "transferSize": 2048, "duration": 10},
        {"name": "https://x/b.css", "initiatorType": "link", "transferSize": 1024, "duration": 5},
    ]
    metrics = webser.compute_network_metrics(entries)
    assert metrics["total_transfer_kb"] == 3.0
    assert metrics["request_count"] == 2
    assert metrics["render_blocking_css"] == 1


def test_compute_network_metrics_handles_empty_and_missing_fields():
    assert webser.compute_network_metrics([]) == {
        "total_transfer_kb": 0.0, "request_count": 0, "render_blocking_css": 0
    }
    metrics = webser.compute_network_metrics([{"name": "https://x/a"}])
    assert metrics["total_transfer_kb"] == 0.0 and metrics["request_count"] == 1


def test_css_with_query_string_counts_as_render_blocking():
    entries = [{"name": "https://x/app.css?v=3", "initiatorType": "other", "transferSize": 500}]
    assert webser.compute_network_metrics(entries)["render_blocking_css"] == 1


def test_zero_byte_css_not_counted_as_render_blocking():
    entries = [{"name": "https://x/app.css", "initiatorType": "link", "transferSize": 0}]
    assert webser.compute_network_metrics(entries)["render_blocking_css"] == 0


def test_compute_resource_timings_maps_canonical_shape():
    entries = [
        {"name": "https://x/hero.mp4", "initiatorType": "media",
         "transferSize": 2048, "duration": 390.4444},
        {"name": "", "initiatorType": "script", "transferSize": 10},  # dropped: no name
    ]
    timings = webser.compute_resource_timings(entries)
    assert len(timings) == 1
    assert timings[0] == {
        "name": "https://x/hero.mp4", "type": "media",
        "transfer_kb": 2.0, "duration_ms": 390.444,
    }


def test_resource_timing_defaults_type_to_other():
    timings = webser.compute_resource_timings([{"name": "https://x/a", "transferSize": 0}])
    assert timings[0]["type"] == "other"


def test_collect_web_vitals_coerces_and_nulls_bad_values():
    class P:
        def evaluate(self, script, arg=None):
            return {"lcp_ms": "6200", "cls": None, "inp_ms": "bad", "fcp_ms": 3100}

    vitals = webser.collect_web_vitals(P())
    assert vitals["lcp_ms"] == 6200.0   # numeric string coerced
    assert vitals["cls"] is None
    assert vitals["inp_ms"] is None      # non-numeric -> None, never fabricated
    assert vitals["ttfb_ms"] is None     # absent key -> None


def test_collector_script_is_a_self_invoking_iife():
    """add_init_script executes source; a bare arrow function would never run."""
    script = webser.COLLECTOR_SCRIPT.strip()
    assert script.startswith("(()") and script.endswith(")();")


# --- LCP candidates with no exposable timing ------------------------------- #
# A cross-origin resource served without `Timing-Allow-Origin` can produce an
# LCP entry whose renderTime AND loadTime are both 0, so startTime is 0 (seen
# on a hero <video>). Zero is an absence of timing, not a 0 ms paint: letting
# it overwrite a real earlier candidate reports the page as instant.

def test_lcp_underestimated_when_larger_candidate_has_no_timing():
    assert webser.lcp_underestimated(
        {"lcp_timed_max_size": 25742, "lcp_untimed_max_size": 1123200}
    ) is True


def test_lcp_not_underestimated_when_largest_candidate_is_timed():
    assert webser.lcp_underestimated(
        {"lcp_timed_max_size": 61440, "lcp_untimed_max_size": 5670}
    ) is False


def test_lcp_not_underestimated_when_no_untimed_candidates():
    assert webser.lcp_underestimated({"lcp_timed_max_size": 9010}) is False
    assert webser.lcp_underestimated({}) is False


def test_collect_web_vitals_flags_underestimated_lcp():
    class P:
        def evaluate(self, script, arg=None):
            return {
                "lcp_ms": 1164.7,
                "cls": 0.01,
                "inp_ms": 120,
                "fcp_ms": 900,
                "lcp_timed_max_size": 25742,
                "lcp_untimed_max_size": 1123200,
            }

    vitals = webser.collect_web_vitals(P())
    # The largest timed candidate survives — never clobbered to 0.
    assert vitals["lcp_ms"] == 1164.7
    assert vitals["lcp_underestimated"] is True


def test_collect_web_vitals_lcp_underestimated_defaults_false():
    class P:
        def evaluate(self, script, arg=None):
            return {"lcp_ms": 2200, "cls": 0.0, "inp_ms": 90}

    assert webser.collect_web_vitals(P())["lcp_underestimated"] is False


def test_collector_script_ignores_zero_start_time_lcp_candidates():
    """The guard must live in the JS: a 0 startTime may not become lcp_ms."""
    script = webser.COLLECTOR_SCRIPT
    assert "e.startTime > 0" in script
    assert "lcp_untimed_max_size" in script
    assert "lcp_timed_max_size" in script


def test_read_script_exposes_lcp_candidate_sizes():
    assert "lcp_timed_max_size" in webser.READ_SCRIPT
    assert "lcp_untimed_max_size" in webser.READ_SCRIPT


@pytest.mark.parametrize(
    "script",
    [webser.READ_SCRIPT, webser.RESOURCE_ENTRIES_SCRIPT, webser.SAFE_CLICK_POINT_SCRIPT],
)
def test_evaluate_scripts_are_expressions_not_return_statements(script):
    """`return ...` raises 'Illegal return statement' in a real browser."""
    assert script.strip().startswith("()")


def test_collect_all_reads_vitals_and_resources():
    log = []
    page = FakePage(log, entries=[
        {"name": "https://x/a.js", "initiatorType": "script", "transferSize": 1024, "duration": 3}
    ])
    out = webser.collect_all(page)
    assert out["cwp"]["lcp_ms"] == 100
    assert out["network"]["request_count"] == 1
    assert out["resource_timings"][0]["name"] == "https://x/a.js"


def test_trigger_interaction_presses_escape_and_clicks_safe_point():
    log = []
    page = FakePage(log)
    webser.trigger_interaction(page)
    assert page.keyboard.presses == ["Escape"]
    assert page.mouse.clicks == [(10, 20)]


def test_trigger_interaction_is_best_effort_on_failure():
    class Broken:
        keyboard = property(lambda self: (_ for _ in ()).throw(RuntimeError("no kb")))

        def evaluate(self, script, arg=None):
            raise RuntimeError("no eval")

    webser.trigger_interaction(Broken())  # must not raise


def test_wait_helpers_return_false_on_timeout():
    class P:
        def wait_for_function(self, expression, **kwargs):
            raise TimeoutError("timed out")

    assert webser.wait_for_lcp(P(), timeout_ms=1) is False
    assert webser.wait_for_inp(P(), timeout_ms=1) is False


# --------------------------------------------------------------------------- #
# Total Blocking Time (lab responsiveness, derived from long tasks)
# --------------------------------------------------------------------------- #
def test_tbt_sums_blocking_time_over_50ms_floor():
    tasks = [
        {"start": 1000, "duration": 120},  # 70ms blocking
        {"start": 1200, "duration": 80},   # 30ms blocking
    ]
    assert webser.compute_tbt_ms(tasks, fcp_ms=500) == 100.0


def test_tbt_ignores_tasks_at_or_below_the_floor():
    assert webser.compute_tbt_ms([{"start": 900, "duration": 50}], fcp_ms=500) == 0.0
    assert webser.compute_tbt_ms([{"start": 900, "duration": 20}], fcp_ms=500) == 0.0


def test_tbt_excludes_long_tasks_finishing_before_fcp():
    """Blocking before first paint doesn't block a user who sees nothing yet."""
    tasks = [
        {"start": 100, "duration": 200},   # ends at 300, before FCP -> excluded
        {"start": 900, "duration": 150},   # 100ms blocking -> counted
    ]
    assert webser.compute_tbt_ms(tasks, fcp_ms=500) == 100.0


def test_tbt_counts_task_straddling_fcp():
    tasks = [{"start": 400, "duration": 200}]  # ends at 600, after FCP=500
    assert webser.compute_tbt_ms(tasks, fcp_ms=500) == 150.0


def test_tbt_none_when_longtasks_unsupported():
    assert webser.compute_tbt_ms(None, fcp_ms=500) is None


def test_tbt_zero_when_no_long_tasks_occurred():
    assert webser.compute_tbt_ms([], fcp_ms=500) == 0.0


def test_tbt_tolerates_malformed_entries():
    tasks = [{"start": "x", "duration": "y"}, {"start": 900, "duration": 150}]
    assert webser.compute_tbt_ms(tasks, fcp_ms=500) == 100.0


def test_collect_web_vitals_derives_tbt():
    class P:
        def evaluate(self, script, arg=None):
            return {
                "lcp_ms": 1000, "cls": 0.1, "inp_ms": 128, "fcp_ms": 500,
                "ttfb_ms": 200, "longtasks": [{"start": 600, "duration": 130}],
            }

    assert webser.collect_web_vitals(P())["tbt_ms"] == 80.0


# --------------------------------------------------------------------------- #
# CDP main-thread metrics (DevTools-native, no Lighthouse Node bridge)
# --------------------------------------------------------------------------- #
CDP_PAYLOAD = {"metrics": [
    {"name": "ScriptDuration", "value": 1.2345},       # seconds
    {"name": "LayoutDuration", "value": 0.05},
    {"name": "RecalcStyleDuration", "value": 0.02},
    {"name": "TaskDuration", "value": 2.0},
    {"name": "JSHeapUsedSize", "value": 2048},         # bytes
    {"name": "Nodes", "value": 1500},
    {"name": "LayoutCount", "value": 12},
    {"name": "JSEventListeners", "value": 40},
    {"name": "Resources", "value": 25},
]}


def test_map_metrics_converts_seconds_to_ms_and_bytes_to_kb():
    mapped = cdp_metrics.map_metrics(CDP_PAYLOAD)
    assert mapped["script_ms"] == 1234.5
    assert mapped["layout_ms"] == 50.0
    assert mapped["style_ms"] == 20.0
    assert mapped["task_ms"] == 2000.0
    assert mapped["js_heap_kb"] == 2.0


def test_map_metrics_counts_are_ints():
    mapped = cdp_metrics.map_metrics(CDP_PAYLOAD)
    assert mapped["dom_nodes"] == 1500 and isinstance(mapped["dom_nodes"], int)
    assert mapped["layout_count"] == 12
    assert mapped["js_event_listeners"] == 40
    assert mapped["resource_count"] == 25


def test_map_metrics_missing_counters_are_none_not_zero():
    """'not reported' and 'reported as zero' are different facts."""
    mapped = cdp_metrics.map_metrics({"metrics": []})
    assert set(mapped) == set(cdp_metrics.FIELDS)
    assert all(v is None for v in mapped.values())


def test_map_metrics_tolerates_garbage_payloads():
    assert all(v is None for v in cdp_metrics.map_metrics(None).values())
    mapped = cdp_metrics.map_metrics(
        {"metrics": [{"value": 1}, {"name": "Nodes", "value": "nan-ish"}]}
    )
    assert mapped["dom_nodes"] is None


class RecordingCDP:
    def __init__(self):
        self.sent = []

    def send(self, method, params=None):
        self.sent.append(method)
        return CDP_PAYLOAD if method == "Performance.getMetrics" else None


def test_collect_cdp_metrics_maps_payload():
    cdp = RecordingCDP()
    metrics = cdp_metrics.collect_cdp_metrics(cdp)
    assert cdp.sent == ["Performance.getMetrics"]
    assert metrics["script_ms"] == 1234.5


def test_enable_starts_the_performance_domain():
    cdp = RecordingCDP()
    cdp_metrics.enable(cdp)
    assert cdp.sent == ["Performance.enable"]


def test_performance_domain_enabled_before_navigation(public_dns):
    """Counters only accumulate while enabled — enabling after load reads ~0."""
    browser = FakeBrowser()
    make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    log = browser.log
    assert log.index("cdp:Performance.enable") < log.index("goto")


def test_collect_cdp_metrics_degrades_gracefully():
    class Broken:
        def send(self, method, params=None):
            raise RuntimeError("domain unavailable")

    metrics = cdp_metrics.collect_cdp_metrics(Broken())
    assert all(v is None for v in metrics.values())


def test_runner_collects_cdp_main_thread_metrics(public_dns):
    browser = FakeBrowser()
    runner = make_runner(
        browser, collect_cdp_metrics_fn=lambda cdp: {"script_ms": 1234.5, "dom_nodes": 1500}
    )
    result = runner.run_condition("https://example.com/", DEVICE, NETWORK)
    assert result["main_thread"] == {"script_ms": 1234.5, "dom_nodes": 1500}


def test_lighthouse_is_optional_by_default(public_dns):
    """Default path must not require the Node bridge (it would raise)."""
    browser = FakeBrowser()
    runner = BrowserRunner(browser, collect_metrics_fn=sample_collect)
    result = runner.run_condition("https://example.com/", DEVICE, NETWORK)
    assert result["lighthouse"] == {}


# --------------------------------------------------------------------------- #
# lighthouse
# --------------------------------------------------------------------------- #
def test_category_scores_maps_fractions_to_0_100():
    lhr = {"categories": {
        "performance": {"score": 0.54},
        "accessibility": {"score": 0.88},
        "best-practices": {"score": 0.79},
        "seo": {"score": 0.9},
    }}
    assert lighthouse.category_scores(lhr) == {
        "performance": 54, "accessibility": 88, "best_practices": 79, "seo": 90
    }


def test_category_scores_missing_categories_are_none():
    assert lighthouse.category_scores({}) == {
        "performance": None, "accessibility": None, "best_practices": None, "seo": None
    }


def test_category_scores_null_score_is_none_not_zero():
    scores = lighthouse.category_scores({"categories": {"performance": {"score": None}}})
    assert scores["performance"] is None


def test_run_lighthouse_uses_injected_runner():
    scores = lighthouse.run_lighthouse(
        "https://example.com/", object(),
        runner=lambda url, cdp: {"categories": {"seo": {"score": 1.0}}},
    )
    assert scores["seo"] == 100


def test_default_lighthouse_runner_raises_actionable_error():
    with pytest.raises(lighthouse.LighthouseUnavailableError, match="bridge"):
        lighthouse.run_lighthouse("https://example.com/", object())


# --------------------------------------------------------------------------- #
# campaign: medians, planning, run emission
# --------------------------------------------------------------------------- #
def test_median_ignores_none_values():
    assert automated.median([100, None, 300]) == 200
    assert automated.median([None, None]) is None
    assert automated.median([]) is None


def test_median_int_rounds_interpolated_medians():
    """An even run count interpolates; count fields must stay whole numbers."""
    assert automated.median_int([10, 21]) == 16      # 15.5 -> 16
    assert automated.median_int([10, 20]) == 15
    assert automated.median_int([None]) is None


def test_merge_median_metrics_keeps_count_fields_integral():
    """Regression: 2 runs produced fractional request_count and failed the schema."""
    measurements = [
        {"cwp": {"lcp_ms": 1000, "cls": 0.1, "inp_ms": 100},
         "network": {"request_count": 10, "render_blocking_css": 1},
         "lighthouse": {"performance": 50},
         "main_thread": {"dom_nodes": 100, "script_ms": 10.0}},
        {"cwp": {"lcp_ms": 2000, "cls": 0.2, "inp_ms": 200},
         "network": {"request_count": 21, "render_blocking_css": 2},
         "lighthouse": {"performance": 61},
         "main_thread": {"dom_nodes": 201, "script_ms": 20.0}},
    ]
    merged = automated.merge_median_metrics(measurements)
    assert isinstance(merged["network"]["request_count"], int)
    assert merged["network"]["request_count"] == 16
    assert isinstance(merged["lighthouse"]["performance"], int)
    assert isinstance(merged["main_thread"]["dom_nodes"], int)
    assert merged["main_thread"]["script_ms"] == 15.0  # floats keep precision


def test_even_run_count_still_validates_against_schema():
    """The above, end to end: 2-run condition must emit a valid Run."""
    cfg = make_cfg()
    measurements = [
        {"cwp": {"lcp_ms": 1000, "cls": 0.1, "inp_ms": 100},
         "network": {"request_count": 10}, "main_thread": {"dom_nodes": 100}},
        {"cwp": {"lcp_ms": 2000, "cls": 0.2, "inp_ms": 200},
         "network": {"request_count": 21}, "main_thread": {"dom_nodes": 201}},
    ]
    run = automated.make_automated_run(
        cfg, cfg.pages[0], PageTest(device="mid-mobile", network="slow-4g", runs=2), measurements
    )
    assert run.metrics.network.request_count == 16
    # median(100, 201) = 150.5; Python's round() breaks ties to even -> 150.
    assert run.metrics.main_thread.dom_nodes == 150


def test_merge_median_metrics_across_runs():
    measurements = [
        {"cwp": {"lcp_ms": 1000, "cls": 0.1}, "lighthouse": {"performance": 50},
         "network": {"request_count": 10}},
        {"cwp": {"lcp_ms": 3000, "cls": 0.3}, "lighthouse": {"performance": 70},
         "network": {"request_count": 30}},
        {"cwp": {"lcp_ms": 2000, "cls": 0.2}, "lighthouse": {"performance": 60},
         "network": {"request_count": 20}},
    ]
    merged = automated.merge_median_metrics(measurements)
    assert merged["cwp"]["lcp_ms"] == 2000
    assert merged["cwp"]["cls"] == 0.2
    assert merged["lighthouse"]["performance"] == 60
    assert merged["network"]["request_count"] == 20


def test_median_measurement_picks_run_closest_to_median_lcp():
    measurements = [
        {"cwp": {"lcp_ms": 1000}, "captures": {"screenshot": "a.png"}},
        {"cwp": {"lcp_ms": 3000}, "captures": {"screenshot": "b.png"}},
        {"cwp": {"lcp_ms": 2000}, "captures": {"screenshot": "c.png"}},
    ]
    assert automated.median_measurement(measurements)["captures"]["screenshot"] == "c.png"


def test_median_measurement_handles_empty_and_missing_lcp():
    assert automated.median_measurement([]) == {}
    measurements = [{"cwp": {}}, {"cwp": {}}]
    assert automated.median_measurement(measurements) is measurements[0]


# --------------------------------------------------------------------------- #
# ProjectConfig fixture for planning/campaign tests
# --------------------------------------------------------------------------- #
def make_cfg(pages=None) -> ProjectConfig:
    pages = pages if pages is not None else [
        PageTarget(name="homepage", url="https://example.com/", tests=[
            PageTest(device="mid-mobile", network="slow-4g", runs=3),
            PageTest(device="desktop", network="fast-3g", runs=2),
        ]),
        PageTarget(name="pdp", url="https://example.com/p/1", tests=[
            PageTest(device="mid-mobile", network="slow-4g", runs=1),
        ]),
    ]
    return ProjectConfig(
        settings=Settings(),
        devices={"mid-mobile": DEVICE, "desktop": DESKTOP},
        networks={
            "slow-4g": NETWORK,
            "fast-3g": Network(name="fast-3g", latency_ms=150, downlink_mbps=1.6, uplink_mbps=0.75),
        },
        project="storefront",
        pages=pages,
    )


def test_plan_conditions_expands_full_matrix():
    plan = automated.plan_conditions(make_cfg())
    assert [(p.name, c.device, c.network, c.runs) for p, c in plan] == [
        ("homepage", "mid-mobile", "slow-4g", 3),
        ("homepage", "desktop", "fast-3g", 2),
        ("pdp", "mid-mobile", "slow-4g", 1),
    ]


def test_plan_conditions_pages_filter():
    plan = automated.plan_conditions(make_cfg(), pages=["pdp"])
    assert {p.name for p, _ in plan} == {"pdp"}


def test_plan_conditions_unknown_page_raises():
    with pytest.raises(ValueError, match="Unknown page name"):
        automated.plan_conditions(make_cfg(), pages=["nope"])


def test_plan_conditions_device_override_and_dedupe():
    """Overriding device collapses homepage's two conditions into one."""
    plan = automated.plan_conditions(make_cfg(), device="desktop", network="fast-3g")
    assert [(p.name, c.device, c.network) for p, c in plan] == [
        ("homepage", "desktop", "fast-3g"),
        ("pdp", "desktop", "fast-3g"),
    ]


def test_plan_conditions_runs_override_applies_everywhere():
    plan = automated.plan_conditions(make_cfg(), runs=5)
    assert {c.runs for _, c in plan} == {5}


def test_plan_conditions_rejects_runs_below_one():
    with pytest.raises(ValueError, match="runs"):
        automated.plan_conditions(make_cfg(), runs=0)


def test_make_automated_run_uses_medians_and_device_cpu_throttle():
    cfg = make_cfg()
    measurements = [
        {"cwp": {"lcp_ms": 1000, "cls": 0.1, "inp_ms": 100}},
        {"cwp": {"lcp_ms": 3000, "cls": 0.3, "inp_ms": 300}},
        {"cwp": {"lcp_ms": 2000, "cls": 0.2, "inp_ms": 200}},
    ]
    run = automated.make_automated_run(
        cfg, cfg.pages[0], PageTest(device="mid-mobile", network="slow-4g", runs=3), measurements
    )
    assert run.metrics.cwp.lcp_ms == 2000
    assert run.meta.source == "automated"
    assert run.condition.cpu_throttle == 4  # from the device preset
    assert run.page.name == "homepage"
    assert run.project.name == "storefront"


def test_make_automated_run_rejects_missing_cwv_trio():
    """§4.3 — a partial automated run must fail loudly, not emit silently."""
    cfg = make_cfg()
    measurements = [{"cwp": {"lcp_ms": 1000, "cls": 0.1}}]  # no INP
    with pytest.raises(ValidationError, match="inp_ms"):
        automated.make_automated_run(
            cfg, cfg.pages[0], PageTest(device="mid-mobile", network="slow-4g", runs=1), measurements
        )


def test_merge_median_metrics_ors_lcp_underestimated_across_runs():
    """A flag is not a measurement: it must not be medianed, it must be OR-ed.

    If any run of the condition saw an untimed larger candidate, the reported
    median LCP for that condition is an underestimate.
    """
    merged = automated.merge_median_metrics([
        {"cwp": {"lcp_ms": 1000, "cls": 0.1, "inp_ms": 10, "lcp_underestimated": False}},
        {"cwp": {"lcp_ms": 2000, "cls": 0.2, "inp_ms": 20, "lcp_underestimated": True}},
        {"cwp": {"lcp_ms": 3000, "cls": 0.3, "inp_ms": 30, "lcp_underestimated": False}},
    ])
    assert merged["cwp"]["lcp_underestimated"] is True
    assert merged["cwp"]["lcp_ms"] == 2000


def test_merge_median_metrics_lcp_underestimated_false_when_no_run_flagged():
    merged = automated.merge_median_metrics([
        {"cwp": {"lcp_ms": 1000, "cls": 0.1, "inp_ms": 10}},
        {"cwp": {"lcp_ms": 2000, "cls": 0.2, "inp_ms": 20}},
    ])
    assert merged["cwp"]["lcp_underestimated"] is False


def test_make_automated_run_carries_lcp_underestimated_flag():
    cfg = make_cfg()
    measurements = [
        {"cwp": {"lcp_ms": 1164.7, "cls": 0.1, "inp_ms": 10,
                 "lcp_underestimated": True}},
    ]
    run = automated.make_automated_run(
        cfg, cfg.pages[0], PageTest(device="mid-mobile", network="slow-4g", runs=1), measurements
    )
    assert run.metrics.cwp.lcp_underestimated is True


def test_make_automated_run_carries_representative_artifacts():
    cfg = make_cfg()
    measurements = [
        {"cwp": {"lcp_ms": 1000, "cls": 0.1, "inp_ms": 10},
         "captures": {"screenshot": "a.png"}, "resource_timings": []},
        {"cwp": {"lcp_ms": 3000, "cls": 0.3, "inp_ms": 30},
         "captures": {"screenshot": "b.png"},
         "resource_timings": [{"name": "/x.js", "type": "script", "transfer_kb": 1, "duration_ms": 2}]},
        {"cwp": {"lcp_ms": 2000, "cls": 0.2, "inp_ms": 20},
         "captures": {"screenshot": "c.png"}, "resource_timings": []},
    ]
    run = automated.make_automated_run(
        cfg, cfg.pages[0], PageTest(device="mid-mobile", network="slow-4g", runs=3), measurements
    )
    assert run.captures.screenshot == "c.png"  # median-LCP run


# --------------------------------------------------------------------------- #
# Optional custom request headers (opt-in; see tests/unit/headers_test.py)
# --------------------------------------------------------------------------- #
def test_headers_are_applied_at_context_level(public_dns):
    """Context-level headers cover the document AND every sub-resource.

    Playwright applies ``extra_http_headers`` set on a context to every request
    made by every page in it. Setting them per-navigation would leave scripts,
    images and XHR unprotected — exactly the requests a bot filter blocks.
    """
    browser = FakeBrowser()
    runner = make_runner(browser)
    runner.run_condition(
        "https://www.oakley.com/en-us", DEVICE, NETWORK,
        extra_http_headers={"X-Akamai-Bot": "tok"},
    )
    ctx_kwargs, _ = browser.contexts[0]
    assert ctx_kwargs["extra_http_headers"] == {"X-Akamai-Bot": "tok"}


def test_no_headers_omits_extra_http_headers_key_entirely(public_dns):
    """The optionality guarantee: not configuring headers changes nothing.

    The key must be ABSENT, not present-and-empty — a run without headers must
    construct the browser context exactly as it did before the feature existed.
    """
    browser = FakeBrowser()
    runner = make_runner(browser)
    runner.run_condition("https://example.com/", DEVICE, NETWORK)
    ctx_kwargs, _ = browser.contexts[0]
    assert "extra_http_headers" not in ctx_kwargs


def test_empty_headers_mapping_also_omits_the_key(public_dns):
    browser = FakeBrowser()
    runner = make_runner(browser)
    runner.run_condition("https://example.com/", DEVICE, NETWORK, extra_http_headers={})
    ctx_kwargs, _ = browser.contexts[0]
    assert "extra_http_headers" not in ctx_kwargs


# --------------------------------------------------------------------------- #
# Block detection (403/429) — records always, fails only on the main document
# --------------------------------------------------------------------------- #
def test_successful_run_reports_main_status_and_zero_blocks(public_dns):
    browser = FakeBrowser(page_kwargs={"main_status": 200, "sub_statuses": [200, 200]})
    result = make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    assert result["guard"]["main_status"] == 200
    assert result["guard"]["blocked_requests"] == 0


def test_blocked_sub_resources_are_counted_but_do_not_fail_the_run(public_dns):
    """Running deliberately without a token is supported; a stray 403 is not fatal."""
    browser = FakeBrowser(
        page_kwargs={"main_status": 200, "sub_statuses": [200, 403, 429, 200]}
    )
    result = make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    assert result["guard"]["blocked_requests"] == 2
    assert result["cwp"]["lcp_ms"] == 6200  # measurement still returned


@pytest.mark.parametrize("status", [403, 429, 404, 500])
def test_non_2xx_main_document_fails_the_run(public_dns, status):
    """A block page has real, fast CWV numbers — storing them would poison the report."""
    browser = FakeBrowser(page_kwargs={"main_status": status})
    with pytest.raises(BlockedResponseError) as exc:
        make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    assert str(status) in str(exc.value)


def test_blocked_main_document_still_closes_the_context(public_dns):
    browser = FakeBrowser(page_kwargs={"main_status": 403})
    with pytest.raises(BlockedResponseError):
        make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    _, ctx = browser.contexts[0]
    assert ctx.closed is True


# --------------------------------------------------------------------------- #
# run_campaign
# --------------------------------------------------------------------------- #
class RecordingRunner:
    """Stand-in for BrowserRunner; records calls and returns canned metrics."""

    def __init__(self):
        self.calls = []

    def run_condition(
        self, url, device, network, *, artifacts_dir=None, run_id=None,
        extra_http_headers=None,
    ):
        self.calls.append(
            {"url": url, "device": device.name, "network": network.name,
             "artifacts_dir": artifacts_dir, "run_id": run_id,
             "extra_http_headers": extra_http_headers}
        )
        n = len(self.calls)
        return {
            "cwp": {"lcp_ms": 1000 * n, "cls": 0.1, "inp_ms": 100},
            "lighthouse": {"performance": 50},
            "network": {"request_count": 10},
            "resource_timings": [],
            "captures": {"screenshot": f"{run_id}.png"},
        }


def test_run_campaign_emits_one_run_per_page_condition():
    cfg = make_cfg()
    runner = RecordingRunner()
    runs = automated.run_campaign(cfg, runner)
    assert len(runs) == 3                      # 2 homepage conditions + 1 pdp
    assert len(runner.calls) == 3 + 2 + 1      # N runs summed per condition


def test_run_campaign_passes_resolved_device_and_network_objects():
    cfg = make_cfg()
    runner = RecordingRunner()
    automated.run_campaign(cfg, runner, pages=["pdp"])
    assert runner.calls[0]["device"] == "mid-mobile"
    assert runner.calls[0]["network"] == "slow-4g"
    assert runner.calls[0]["url"] == "https://example.com/p/1"


def test_run_campaign_builds_per_run_artifact_dirs():
    cfg = make_cfg()
    runner = RecordingRunner()
    automated.run_campaign(cfg, runner, pages=["pdp"], artifacts_root="data/raw")
    path = runner.calls[0]["artifacts_dir"].replace("\\", "/")
    assert path == "data/raw/pdp/mid-mobile__slow-4g/run_1"


def test_run_campaign_without_artifacts_root_passes_none():
    cfg = make_cfg()
    runner = RecordingRunner()
    automated.run_campaign(cfg, runner, pages=["pdp"])
    assert runner.calls[0]["artifacts_dir"] is None


def test_run_campaign_passes_no_headers_when_none_configured():
    """Default config declares no headers, so the runner receives none."""
    runner = RecordingRunner()
    automated.run_campaign(make_cfg(), runner, pages=["pdp"])
    assert runner.calls[0]["extra_http_headers"] == {}


def test_run_campaign_resolves_and_passes_configured_headers():
    cfg = make_cfg()
    cfg.headers = {"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}
    runner = RecordingRunner()
    automated.run_campaign(
        cfg, runner, pages=["pdp"], env={"AKAMAI_BOT_TOKEN": "tok"}
    )
    assert runner.calls[0]["extra_http_headers"] == {"X-Akamai-Bot": "tok"}


def test_run_campaign_no_headers_flag_suppresses_configured_headers():
    """`--no-headers` measures the same targets without the allowlist token."""
    cfg = make_cfg()
    cfg.headers = {"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}
    runner = RecordingRunner()
    automated.run_campaign(
        cfg, runner, pages=["pdp"], no_headers=True, env={"AKAMAI_BOT_TOKEN": "tok"}
    )
    assert runner.calls[0]["extra_http_headers"] == {}


def test_run_campaign_no_headers_flag_does_not_require_the_token():
    """With headers suppressed, an unset token must not fail the campaign."""
    cfg = make_cfg()
    cfg.headers = {"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}
    runner = RecordingRunner()
    runs = automated.run_campaign(cfg, runner, pages=["pdp"], no_headers=True, env={})
    assert len(runs) == 1


def test_run_campaign_respects_overrides():
    cfg = make_cfg()
    runner = RecordingRunner()
    runs = automated.run_campaign(cfg, runner, device="desktop", network="fast-3g", runs=1)
    assert len(runs) == 2                       # one per page after dedupe
    assert all(c["device"] == "desktop" for c in runner.calls)
    assert all(r.condition.runs == 1 for r in runs)


def test_run_campaign_runs_are_schema_valid_and_distinct():
    cfg = make_cfg()
    runs = automated.run_campaign(cfg, RecordingRunner())
    assert len({r.run_id for r in runs}) == len(runs)
    for run in runs:
        assert run.metrics.cwp.lcp_ms is not None
        assert run.meta.source == "automated"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_dry_run_prints_plan(monkeypatch, capsys):
    monkeypatch.setattr(automated, "plan_conditions", automated.plan_conditions)
    cfg = make_cfg()
    monkeypatch.setattr("config.load.load_config", lambda *a, **k: cfg)
    code = automated.main(["--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "homepage\tmid-mobile\tslow-4g\t3" in out
    assert "pdp\tmid-mobile\tslow-4g\t1" in out


def test_cli_dry_run_applies_page_and_device_overrides(monkeypatch, capsys):
    monkeypatch.setattr("config.load.load_config", lambda *a, **k: make_cfg())
    code = automated.main(["--dry-run", "--pages", "pdp", "--device", "desktop", "--runs", "7"])
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert out == "pdp\tdesktop\tslow-4g\t7"


def test_cli_unknown_page_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("config.load.load_config", lambda *a, **k: make_cfg())
    code = automated.main(["--dry-run", "--pages", "ghost"])
    assert code == 1
    assert "unknown page" in capsys.readouterr().err.lower()


def test_cli_config_error_returns_nonzero(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("targets.yaml missing")

    monkeypatch.setattr("config.load.load_config", boom)
    code = automated.main(["--dry-run"])
    assert code == 1
    assert "error" in capsys.readouterr().err.lower()


def test_cli_sends_configured_headers_by_default(monkeypatch, tmp_path):
    cfg = make_cfg()
    cfg.headers = {"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}
    monkeypatch.setenv("AKAMAI_BOT_TOKEN", "tok")
    monkeypatch.setattr("config.load.load_config", lambda *a, **k: cfg)
    runner = RecordingRunner()
    monkeypatch.setattr(automated, "_real_runner", lambda cfg=None: (None, None, runner))

    assert automated.main(["--pages", "pdp", "--output-dir", str(tmp_path)]) == 0
    assert runner.calls[0]["extra_http_headers"] == {"X-Akamai-Bot": "tok"}


def test_cli_no_headers_flag_suppresses_them(monkeypatch, tmp_path):
    cfg = make_cfg()
    cfg.headers = {"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}
    monkeypatch.setenv("AKAMAI_BOT_TOKEN", "tok")
    monkeypatch.setattr("config.load.load_config", lambda *a, **k: cfg)
    runner = RecordingRunner()
    monkeypatch.setattr(automated, "_real_runner", lambda cfg=None: (None, None, runner))

    assert automated.main(
        ["--pages", "pdp", "--no-headers", "--output-dir", str(tmp_path)]
    ) == 0
    assert runner.calls[0]["extra_http_headers"] == {}


def test_cli_reports_missing_token_without_leaking_it(monkeypatch, tmp_path, capsys):
    """An unset token is a clear config error, not a stack trace."""
    cfg = make_cfg()
    cfg.headers = {"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}
    monkeypatch.delenv("AKAMAI_BOT_TOKEN", raising=False)
    # main() loads .env; without this the developer's own token would leak into
    # the test environment and undo the delenv above.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("config.load.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(automated, "_real_runner", lambda cfg=None: (None, None, RecordingRunner()))

    code = automated.main(["--pages", "pdp", "--output-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 1
    assert "AKAMAI_BOT_TOKEN" in err


def test_cli_writes_one_json_per_run(monkeypatch, tmp_path, capsys):
    cfg = make_cfg()
    monkeypatch.setattr("config.load.load_config", lambda *a, **k: cfg)
    runner = RecordingRunner()
    monkeypatch.setattr(automated, "_real_runner", lambda cfg=None: (None, None, runner))

    code = automated.main([
        "--pages", "pdp", "--output-dir", str(tmp_path), "--artifacts-root", str(tmp_path / "raw"),
    ])
    assert code == 0
    written = sorted(p.name for p in tmp_path.glob("*.json"))
    assert written == ["pdp__mid-mobile__slow-4g.json"]
    data = json.loads((tmp_path / written[0]).read_text(encoding="utf-8"))
    assert data["page"]["name"] == "pdp"
    assert data["meta"]["source"] == "automated"
    assert data["metrics"]["cwp"]["inp_ms"] == 100
