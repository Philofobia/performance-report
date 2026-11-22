"""Web-vitals + network capture helpers (mockable).

``install_collector`` must run **before navigation** (Playwright
``add_init_script``): LCP/CLS/FCP are buffered observer metrics, so a collector
injected after ``load`` would miss the very entries we are measuring.
``collect_all`` then reads the accumulated state off the page.

CWV are captured with **native ``PerformanceObserver``** rather than the
``web-vitals`` npm library: no CDN fetch (which the SSRF policy in
SECURITY_PLAN.md §2.2 would have to whitelist), no bundling step, and identical
underlying entry types.

**INP caveat (lab measurement):** INP only exists once a real interaction has
occurred. A lab page load has none, so ``run_condition`` drives a *synthetic*
interaction (see ``ingest/browser/runner.py``) to elicit one. When no
interaction entry is produced, ``inp_ms`` stays ``None`` and the automated run
fails canonical validation loudly rather than reporting a fabricated number.

The pure parsing / arithmetic helpers (network metrics, resource timings, unit
conversions) are completely unit-testable without a browser.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

# Installed via add_init_script BEFORE navigation so buffered entries are caught.
# NOTE: add_init_script takes raw JS source that is *executed*, unlike evaluate
# which takes an expression — so this must be a self-invoking IIFE. A bare arrow
# function here would merely be defined and never run (observers never attach).
COLLECTOR_SCRIPT = """
(() => {
  if (window.__PERF_CAPTURE__) return;
  const state = { lcp_ms: null, cls: 0, inp_ms: null, fcp_ms: null, longtasks: [],
                  lcp_timed_max_size: 0, lcp_untimed_max_size: 0 };
  window.__PERF_CAPTURE__ = state;
  const observe = (type, handler, extra) => {
    try {
      new PerformanceObserver((list) => { list.getEntries().forEach(handler); })
        .observe(Object.assign({ type: type, buffered: true }, extra || {}));
    } catch (e) { /* entry type unsupported in this browser — leave null */ }
  };
  // Last LCP candidate wins (the spec's "largest so far" semantics) — but only
  // if it carries timing. A cross-origin resource served without
  // `Timing-Allow-Origin` can yield an entry whose renderTime AND loadTime are
  // both 0, so startTime is 0 (observed on a hero <video>). That 0 is an
  // absence of timing, not a 0 ms paint; letting it overwrite a real earlier
  // candidate reports the page as having painted instantly. Keep the largest
  // *timed* candidate and record the sizes, so the reader can be told the
  // value is a lower bound rather than being quietly misled.
  observe('largest-contentful-paint', (e) => {
    if (e.startTime > 0) {
      state.lcp_ms = e.startTime;
      // A load time but no render time: startTime is the image's *download*,
      // not its paint (older Chromium, cross-origin, no Timing-Allow-Origin).
      state.lcp_paint_missing = !(e.renderTime > 0) && e.loadTime > 0;
      const el = e.element;
      const cls = el && typeof el.className === 'string' ? el.className.trim().split(/\\s+/)[0] : '';
      const tail = e.url ? ' …/' + e.url.split('?')[0].split('/').pop() : '';
      state.lcp_element = el ? (el.tagName + (el.id ? '#' + el.id : cls ? '.' + cls : '') + tail).slice(0, 300) : null;
      if (e.size > state.lcp_timed_max_size) state.lcp_timed_max_size = e.size;
    } else if (e.size > state.lcp_untimed_max_size) {
      state.lcp_untimed_max_size = e.size;
    }
  });
  // CLS = sum of layout shifts not tied to a recent user input.
  observe('layout-shift', (e) => { if (!e.hadRecentInput) state.cls += e.value; });
  observe('paint', (e) => {
    if (e.name === 'first-contentful-paint') state.fcp_ms = e.startTime;
  });
  // INP ~ the worst interaction latency; only entries with an interactionId count.
  observe('event', (e) => {
    if (!e.interactionId) return;
    if (state.inp_ms === null || e.duration > state.inp_ms) state.inp_ms = e.duration;
  }, { durationThreshold: 16 });
  // Long tasks feed Total Blocking Time — the lab responsiveness metric.
  observe('longtask', (e) => {
    state.longtasks.push({ start: e.startTime, duration: e.duration });
  });
})();
"""

# Long tasks below this are not "blocking" by definition (DevTools/WebPageTest).
BLOCKING_TASK_FLOOR_MS = 50.0

# TTFB comes from the navigation entry at read time, not from an observer.
READ_SCRIPT = """
() => {
  const s = window.__PERF_CAPTURE__ || {};
  const nav = performance.getEntriesByType('navigation')[0];
  return {
    lcp_ms: s.lcp_ms === undefined ? null : s.lcp_ms,
    cls: s.cls === undefined ? null : s.cls,
    inp_ms: s.inp_ms === undefined ? null : s.inp_ms,
    fcp_ms: s.fcp_ms === undefined ? null : s.fcp_ms,
    ttfb_ms: nav ? nav.responseStart : null,
    longtasks: s.longtasks || [],
    interaction_at: s.interaction_at === undefined ? null : s.interaction_at,
    lcp_paint_missing: s.lcp_paint_missing === true,
    lcp_element: s.lcp_element || null,
    lcp_timed_max_size: s.lcp_timed_max_size || 0,
    lcp_untimed_max_size: s.lcp_untimed_max_size || 0
  };
}
"""

RESOURCE_ENTRIES_SCRIPT = """
() => performance.getEntriesByType('resource').map((e) => ({
  name: e.name,
  initiatorType: e.initiatorType,
  transferSize: e.transferSize,
  duration: e.duration
}))
"""

# Finds a viewport point that hits no interactive element, so the synthetic
# interaction used to elicit INP cannot navigate away and reset our measurement.
SAFE_CLICK_POINT_SCRIPT = """
() => {
  const interactive = 'a,button,input,select,textarea,label,summary,[onclick],[role="button"],[role="link"]';
  const w = window.innerWidth, h = window.innerHeight;
  for (let fy = 0.5; fy <= 0.95; fy += 0.15) {
    for (let fx = 0.5; fx <= 0.95; fx += 0.15) {
      const x = Math.round(w * fx), y = Math.round(h * fy);
      const el = document.elementFromPoint(x, y);
      if (el && !el.closest(interactive)) return { x: x, y: y };
    }
  }
  return null;
}
"""

CWP_KEYS = ("lcp_ms", "cls", "inp_ms", "fcp_ms", "ttfb_ms", "tbt_ms")
NETWORK_KEYS = ("total_transfer_kb", "request_count", "render_blocking_css")

# Resource initiator types / URL suffixes that indicate render-blocking CSS.
_CSS_INITIATORS = ("link", "css")
_CSS_SUFFIXES = (".css",)


def install_collector(page) -> None:
    """Install the CWV collector so it runs before any page script.

    Must be called *before* ``page.goto`` — buffered LCP/CLS/FCP entries are
    otherwise emitted before an observer exists to receive them.
    """
    page.add_init_script(COLLECTOR_SCRIPT)


#: Time to Interactive is the end of the last long task before this long a
#: stretch with none (the standard TTI quiet window).
TTI_QUIET_WINDOW_MS = 5000.0


#: Stamps when the synthetic interaction begins (see ``trigger_interaction``).
MARK_INTERACTION_SCRIPT = """
() => { if (window.__PERF_CAPTURE__) { window.__PERF_CAPTURE__.interaction_at = performance.now(); } }
"""


def compute_tbt_ms(
    longtasks: List[Dict[str, Any]], fcp_ms: Optional[float],
    *, until_ms: Optional[float] = None,
) -> Optional[float]:
    """Total Blocking Time: blocking time of long tasks between FCP and TTI.

    Each long task contributes ``duration - 50ms`` (the portion the main thread
    was unavailable to respond). Tasks before FCP are excluded, and so is every
    task after Time to Interactive — the end of the last long task before a
    five-second window with none — matching how DevTools and
    WebPageTest define the metric. This is TTI's CPU criterion only; the
    network criterion needs request timing this collector does not keep.

    Without the TTI bound TBT grew with the observation window: on the live
    Oakley homepage trackers fire long tasks for 20-30 s after load, and the
    unbounded sum read 10.7 s against 0.4-0.65 s in WebPageTest. When no quiet
    window arrives before observation ends, every task counts — TTI is then
    later than we watched.

    ``until_ms`` is when the runner's synthetic interaction began: the key
    press and click that measure INP run the page's handlers, and those tasks
    are ours, not the load's. Returns ``None`` when the browser reported no
    ``longtask`` support at all.
    """
    if longtasks is None:
        return None
    tasks = []
    for task in longtasks:
        try:
            start = float(task.get("start", 0.0))
            duration = float(task.get("duration", 0.0))
        except (TypeError, ValueError, AttributeError):
            continue
        if fcp_ms is not None and start + duration < fcp_ms:
            continue  # entirely before first paint — not blocking the user yet
        if until_ms is not None and start >= until_ms:
            continue  # our own synthetic interaction, not the page load
        tasks.append((start, duration))

    total = 0.0
    quiet_since = fcp_ms if fcp_ms is not None else 0.0
    for start, duration in sorted(tasks):
        if start - quiet_since >= TTI_QUIET_WINDOW_MS:
            break  # interactive: nothing after this is load-time blocking
        blocking = duration - BLOCKING_TASK_FLOOR_MS
        if blocking > 0:
            total += blocking
        quiet_since = max(quiet_since, start + duration)
    return round(total, 3)


def lcp_underestimated(raw: Dict[str, Any]) -> bool:
    """True when a larger LCP candidate existed that exposed no timing.

    Chrome reports ``startTime == 0`` for an LCP candidate whose resource is
    cross-origin and served without ``Timing-Allow-Origin``. When such a
    candidate is *larger* than every candidate that did report a time, the
    element that actually decides the page's LCP was never timed, and the value
    we can report is the largest timed element — a lower bound.

    Also true when the winning candidate had a load time but no render time
    (``lcp_paint_missing``): its ``startTime`` is when the image *downloaded*,
    not when it was painted. Chromium 130 did this for the cross-origin Oakley
    hero, reporting 1192 ms for an image painted — per OAK-39155 — about a
    second later; Chromium 151 exposes the paint (1344 ms).
    """
    def _size(key: str) -> float:
        try:
            return float(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    if raw.get("lcp_paint_missing") is True:
        return True
    return _size("lcp_untimed_max_size") > _size("lcp_timed_max_size")


def collect_web_vitals(page) -> Dict[str, Any]:
    """Read captured CWV values off the page (LCP/CLS/INP/FCP/TTFB/TBT)."""
    raw = page.evaluate(READ_SCRIPT) or {}
    out: Dict[str, Any] = {}
    for key in CWP_KEYS:
        val = raw.get(key)
        try:
            out[key] = None if val is None else float(val)
        except (TypeError, ValueError):
            out[key] = None
    # TBT is derived, not observed directly.
    out["tbt_ms"] = compute_tbt_ms(raw.get("longtasks"), out.get("fcp_ms"),
                                   until_ms=raw.get("interaction_at"))
    # Not a measurement — a qualifier on lcp_ms. See lcp_underestimated().
    out["lcp_underestimated"] = lcp_underestimated(raw)
    element = raw.get("lcp_element")
    out["lcp_element"] = str(element)[:300] if element else None
    return out


def collect_resource_entries(page) -> List[Dict[str, Any]]:
    """Read resource timing entries off the page as plain dicts."""
    return page.evaluate(RESOURCE_ENTRIES_SCRIPT) or []


def _is_render_blocking_css(entry: Dict[str, Any]) -> bool:
    initiator = (entry.get("initiatorType") or "").lower()
    # Compare against the path only: `/app.css?v=3` is still a stylesheet.
    path = urlsplit((entry.get("name") or "").lower()).path
    if initiator in _CSS_INITIATORS or path.endswith(_CSS_SUFFIXES):
        # Only count actual stylesheet loads, not preflight/favicon noise.
        if (entry.get("transferSize") or 0) > 0:
            return True
    return False


def compute_network_metrics(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate resource entries into transfer/request/CSS metrics.

    ``total_transfer_kb`` = sum of transferSize/1024; ``request_count`` = number
    of resource entries; ``render_blocking_css`` = count of stylesheet loads.
    """
    total_bytes = sum(int(e.get("transferSize") or 0) for e in entries)
    blocking_css = sum(1 for e in entries if _is_render_blocking_css(e))
    return {
        "total_transfer_kb": round(total_bytes / 1024.0, 3),
        "request_count": len(entries),
        "render_blocking_css": blocking_css,
    }


def compute_resource_timings(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map resource entries to canonical ``ResourceTiming``-shaped dicts."""
    timings = []
    for e in entries:
        name = e.get("name")
        if not name:
            continue
        timings.append(
            {
                "name": name,
                "type": e.get("initiatorType") or "other",
                "transfer_kb": round(int(e.get("transferSize") or 0) / 1024.0, 3),
                "duration_ms": round(float(e.get("duration") or 0.0), 3),
            }
        )
    return timings


def _wait_for_field(page, field: str, timeout_ms: int) -> bool:
    """Best-effort poll until ``__PERF_CAPTURE__[field]`` is populated.

    Observer callbacks are dispatched asynchronously *after* ``load``, so reading
    the state immediately races them. Returns True if the field arrived; a
    timeout is not an error — the metric is simply reported as unavailable.
    """
    try:
        page.wait_for_function(
            "() => window.__PERF_CAPTURE__ "
            f"&& window.__PERF_CAPTURE__.{field} !== null",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def wait_for_lcp(page, *, timeout_ms: int = 3_000) -> bool:
    """Wait for the LCP observer to report. Call before any interaction.

    LCP stops updating at the first user interaction, so the synthetic INP
    interaction must not run until the largest candidate has been recorded.
    """
    return _wait_for_field(page, "lcp_ms", timeout_ms)


def wait_for_inp(page, *, timeout_ms: int = 1_000) -> bool:
    """Wait for an interaction entry to be dispatched after a synthetic event."""
    return _wait_for_field(page, "inp_ms", timeout_ms)


def trigger_interaction(page) -> None:
    """Drive a synthetic interaction so an INP entry can be measured.

    Presses Escape (never activates a control or submits a form) and, when a
    provably non-interactive point exists, taps it. Both are best-effort: any
    failure leaves ``inp_ms`` as ``None`` rather than raising.

    The moment is recorded first, so Total Blocking Time can leave out the
    long tasks this interaction causes (``compute_tbt_ms``'s ``until_ms``).
    """
    try:
        page.evaluate(MARK_INTERACTION_SCRIPT)
    except Exception:  # pragma: no cover - defensive, browser-specific
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:  # pragma: no cover - defensive, browser-specific
        pass
    try:
        point = page.evaluate(SAFE_CLICK_POINT_SCRIPT)
        if point:
            page.mouse.click(point["x"], point["y"])
    except Exception:  # pragma: no cover - defensive, browser-specific
        pass


def collect_all(page) -> Dict[str, Any]:
    """Full measurement set for a page: CWV + network + resource timings.

    Assumes :func:`install_collector` ran before navigation.
    """
    cwp = collect_web_vitals(page)
    entries = collect_resource_entries(page)
    network = compute_network_metrics(entries)
    timings = compute_resource_timings(entries)
    return {"cwp": cwp, "network": network, "resource_timings": timings}
