"""Playwright lifecycle for one test condition.

Owns the browser -> context (device emulation is applied at *context* creation)
-> page, applies CDP network + CPU-throttling presets from ``config/devices.yaml``
and ``config/networks.yaml``, and captures HAR, trace and screenshots. Returns a
raw measurement dict consumed by ``ingest.automated``.

SECURITY (SECURITY_PLAN.md §2.2 / `security` skill): every user-supplied URL is
passed through ``normalize.url_safety.validate_url(url, resolve=True)`` BEFORE
any navigation — rejecting non-https, private/internal/IP ranges (SSRF).

**Optional custom request headers.** ``extra_http_headers`` (e.g. a bot-allowlist
token for a site behind Akamai) is applied at *context* level, so it covers the
document and every sub-resource — scripts, images, XHR — which is precisely what
a bot filter inspects. It is entirely opt-in: when no headers are supplied the
key is not added to the context kwargs at all, and the context is built exactly
as it was before the feature existed.

Mockability: the Playwright surface (browser/context/page/CDP session) and the
measurement collectors are injected. Tests supply fakes + canned metrics, so no
real browser is ever launched in offline tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from config.load import Device, Network
from normalize import url_safety
from ingest.browser import cdp_metrics, webser, lighthouse


# Statuses a bot filter (Akamai et al.) returns when it rejects a request.
BLOCKED_STATUSES = frozenset({403, 429})


class BlockedResponseError(RuntimeError):
    """The main document did not return 2xx — the measurement is not of the site.

    Raised rather than returned: a block/error page produces real, fast CWV
    numbers, and storing them would silently poison both the report and the
    accumulated RAG findings.
    """


def _no_lighthouse(url: str, cdp: object) -> dict:
    """Default: no Lighthouse audit.

    Main-thread data comes from CDP directly (``cdp_metrics``), so the Node
    Lighthouse bridge is opt-in rather than required. Pass ``run_lighthouse_fn``
    to populate the optional Lighthouse category scores.
    """
    return {}

# Default CDP emulation for a given device (Playwright context.new_context kwargs).
def device_context_kwargs(device: Device) -> Dict[str, Any]:
    """Resolve a config ``Device`` to Playwright ``new_context`` keyword args."""
    kwargs: Dict[str, Any] = {
        "viewport": {"width": device.viewport_width, "height": device.viewport_height},
        "device_scale_factor": device.device_scale_factor,
        "is_mobile": device.mobile,
        "has_touch": device.mobile,
        "user_agent": device.user_agent,
    }
    if kwargs["user_agent"] is None:
        del kwargs["user_agent"]
    return kwargs


def network_throttle_params(network: Network) -> Dict[str, Any]:
    """CDP ``Network.emulateNetworkConditions`` params (bytes/sec)."""
    def _to_bytes(mbps: Optional[float]) -> int:
        # -1 tells Chrome "unthrottled". Mb/s -> bytes/sec (8 bits / byte).
        return -1 if mbps is None else int(round(mbps * 1_000_000 / 8))

    return {
        "offline": network.offline,
        "latency": int(network.latency_ms),
        "downloadThroughput": _to_bytes(network.downlink_mbps),
        "uploadThroughput": _to_bytes(network.uplink_mbps),
    }


def apply_cpu_throttle(cdp, device: Device) -> None:
    """Apply the device's CPU throttle rate via CDP."""
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": device.cpu_throttle})


def apply_network_throttle(cdp, network: Network) -> None:
    """Apply the network preset via CDP."""
    cdp.send("Network.emulateNetworkConditions", network_throttle_params(network))


def _default_cdp_session(context, page):
    """Default CDP session provider (Playwright's new_cdp_session)."""
    return context.new_cdp_session(page)


class BrowserRunner:
    """One Playwright browser; run one (device x network) condition per call."""

    def __init__(
        self,
        browser,
        *,
        cdp_session_factory: Optional[Callable[[object, object], object]] = None,
        install_collector_fn: Optional[Callable[[object], None]] = None,
        collect_metrics_fn: Optional[Callable[[object], dict]] = None,
        trigger_interaction_fn: Optional[Callable[[object], None]] = None,
        setup_page_fn: Optional[Callable[[object], None]] = None,
        collect_cdp_metrics_fn: Optional[Callable[[object], dict]] = None,
        run_lighthouse_fn: Optional[Callable[[str, object], dict]] = None,
        navigation_timeout_ms: int = 30_000,
        network_idle_timeout_ms: int = 5_000,
        lcp_timeout_ms: int = 3_000,
        inp_timeout_ms: int = 1_000,
        measure_inp: bool = True,
    ) -> None:
        self._browser = browser
        self._cdp_factory = cdp_session_factory or _default_cdp_session
        self._install_collector = install_collector_fn or webser.install_collector
        self._collect_metrics = collect_metrics_fn or webser.collect_all
        self._trigger_interaction = trigger_interaction_fn or webser.trigger_interaction
        # Optional per-page setup before navigation: auth cookies, request
        # routing (e.g. excluding third-party noise), extra headers.
        self._setup_page = setup_page_fn
        self._wait_for_lcp = webser.wait_for_lcp
        self._wait_for_inp = webser.wait_for_inp
        self._collect_cdp_metrics = collect_cdp_metrics_fn or cdp_metrics.collect_cdp_metrics
        # Lighthouse is opt-in: CDP supplies the main-thread breakdown natively.
        self._run_lighthouse = run_lighthouse_fn or _no_lighthouse
        self._navigation_timeout_ms = navigation_timeout_ms
        self._network_idle_timeout_ms = network_idle_timeout_ms
        self._lcp_timeout_ms = lcp_timeout_ms
        self._inp_timeout_ms = inp_timeout_ms
        self._measure_inp = measure_inp

    def run_condition(
        self,
        url: str,
        device: Device,
        network: Network,
        *,
        artifacts_dir: Optional[str] = None,
        run_id: Optional[str] = None,
        extra_http_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Run one (url, device, network) measurement and return raw measurements.

        Returns a dict with ``cwp``, ``main_thread``, ``network``,
        ``resource_timings``, ``lighthouse``, ``captures`` and ``guard`` keys.

        ``extra_http_headers`` is optional; when falsy, nothing about context
        construction changes. Raises :class:`BlockedResponseError` if the main
        document returns a non-2xx status.
        """
        # --- SSRF gate: validate BEFORE any navigation (non-negotiable) ---
        url = url_safety.validate_url(url, resolve=True)

        run_token = run_id or "run"
        ctx_kwargs = device_context_kwargs(device)

        # Opt-in: only add the key when headers were actually supplied, so a
        # header-less run builds an identical context to before this existed.
        if extra_http_headers:
            ctx_kwargs["extra_http_headers"] = dict(extra_http_headers)

        har_path: Optional[str] = None
        if artifacts_dir:
            out = Path(artifacts_dir)
            out.mkdir(parents=True, exist_ok=True)
            har_path = str(out / f"{run_token}.har")
            ctx_kwargs["record_har_path"] = har_path
            # SECURITY_PLAN.md §2.6: response bodies can carry tokens/PII and we
            # only need sizes + timings. Header scrubbing happens in store/.
            ctx_kwargs["record_har_content"] = "omit"

        context = self._browser.new_context(**ctx_kwargs)
        try:
            page = context.new_page()
            cdp = self._cdp_factory(context, page)

            # CDP throttling: network + CPU presets from config.
            apply_network_throttle(cdp, network)
            apply_cpu_throttle(cdp, device)

            # Start DevTools counters before navigating, or they accumulate
            # nothing for the page load we are measuring.
            cdp_metrics.enable(cdp)

            # Count filter rejections across every sub-resource. Recorded
            # always: it is the signal that tells you whether an allowlist
            # header was accepted (see docs/CUSTOM_HEADERS.md).
            blocked = {"count": 0}

            def _on_response(response) -> None:
                try:
                    if response.status in BLOCKED_STATUSES:
                        blocked["count"] += 1
                except Exception:  # pragma: no cover - defensive
                    pass

            page.on("response", _on_response)

            if self._setup_page is not None:
                self._setup_page(page)

            # Install the CWV collector BEFORE navigating — LCP/CLS/FCP are
            # buffered observer entries and are lost if we attach after load.
            self._install_collector(page)

            trace_path: Optional[str] = None
            screenshot_path: Optional[str] = None
            if artifacts_dir:
                out = Path(artifacts_dir)
                trace_path = str(out / f"{run_token}.trace.zip")
                # Trace + screenshot capture.
                context.tracing.start(screenshots=True, snapshots=True)

            response = page.goto(
                url, wait_until="load", timeout=self._navigation_timeout_ms
            )
            main_status = getattr(response, "status", None) if response else None
            # Fail fast, before spending the LCP/INP settle time on a block page.
            if main_status is not None and not 200 <= main_status < 300:
                raise BlockedResponseError(
                    f"Main document returned HTTP {main_status} for {url}. "
                    "The page measured is an error/block page, not the site. "
                    "If the target is behind a bot filter, configure the "
                    "allowlist header in config/targets.yaml."
                )

            if not network.offline:
                # Best-effort settle, exactly like the LCP/INP waits below:
                # `load` has already fired, so networkidle only buys late
                # resources a chance to land. A commerce page with continuous
                # analytics beacons may never reach it, and aborting there
                # would discard the entire campaign — every condition already
                # measured included — over a wait that was an optimisation.
                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=self._network_idle_timeout_ms
                    )
                except Exception:
                    pass

            # LCP is dispatched asynchronously after load, and freezes at the
            # first interaction — so settle it BEFORE interacting.
            self._wait_for_lcp(page, timeout_ms=self._lcp_timeout_ms)

            # INP requires a real interaction; a lab page load has none.
            if self._measure_inp:
                self._trigger_interaction(page)
                self._wait_for_inp(page, timeout_ms=self._inp_timeout_ms)

            collected = self._collect_metrics(page) or {}
            # DevTools main-thread counters, read directly over CDP.
            main_thread = self._collect_cdp_metrics(cdp) or {}
            lh_scores = self._run_lighthouse(url, cdp) or {}

            if artifacts_dir:
                out = Path(artifacts_dir)
                screenshot_path = str(out / f"{run_token}.png")
                page.screenshot(path=screenshot_path, full_page=False)
                context.tracing.stop(path=trace_path)
        finally:
            context.close()

        return {
            "cwp": collected.get("cwp", {}),
            "main_thread": main_thread,
            "network": collected.get("network", {}),
            "resource_timings": collected.get("resource_timings", []),
            "lighthouse": lh_scores,
            "guard": {
                "main_status": main_status,
                "blocked_requests": blocked["count"],
            },
            "captures": {
                "screenshot": screenshot_path,
                "har": har_path,
                "trace": trace_path,
            },
        }

