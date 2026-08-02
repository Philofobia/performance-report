"""Main-thread metrics straight from the Chrome DevTools Protocol.

This is the DevTools-native alternative to the Lighthouse Node bridge: the same
counters the DevTools Performance panel reports, read over CDP
``Performance.getMetrics`` — no Node process, no CLI, no extra dependency.

What it gives us that CWV alone does not: *where the main-thread time went*
(script vs. layout vs. style recalc), plus DOM weight and listener counts. That
is the raw material for the report's "where the problem is" section (§6/§3).

Durations arrive from CDP in **seconds** and are converted to milliseconds here;
sizes arrive in bytes and are converted to KB.

``collect_cdp_metrics`` is the integration point; ``map_metrics`` is pure and
unit-testable against a canned CDP payload with no browser.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# CDP metric name -> (our field name, converter)
_DURATION_FIELDS = {
    "ScriptDuration": "script_ms",
    "LayoutDuration": "layout_ms",
    "RecalcStyleDuration": "style_ms",
    "TaskDuration": "task_ms",
}

_COUNT_FIELDS = {
    "Nodes": "dom_nodes",
    "LayoutCount": "layout_count",
    "JSEventListeners": "js_event_listeners",
    "Resources": "resource_count",
}

FIELDS = tuple(_DURATION_FIELDS.values()) + tuple(_COUNT_FIELDS.values()) + ("js_heap_kb",)


def _as_dict(payload: Any) -> Dict[str, float]:
    """Flatten CDP's ``{"metrics": [{"name": n, "value": v}, ...]}`` payload."""
    if not payload:
        return {}
    entries: List[Dict[str, Any]] = payload.get("metrics") or []
    out: Dict[str, float] = {}
    for entry in entries:
        name = entry.get("name")
        if name is None:
            continue
        try:
            out[name] = float(entry.get("value"))
        except (TypeError, ValueError):
            continue
    return out


def map_metrics(payload: Any) -> Dict[str, Optional[float]]:
    """Map a raw CDP ``Performance.getMetrics`` payload to canonical fields.

    Missing counters map to ``None`` rather than 0 — "not reported" and
    "reported as zero" are different facts and the report must not conflate them.
    """
    raw = _as_dict(payload)
    mapped: Dict[str, Optional[float]] = {}

    for cdp_name, field in _DURATION_FIELDS.items():
        value = raw.get(cdp_name)
        # CDP reports these durations in seconds.
        mapped[field] = None if value is None else round(value * 1000.0, 3)

    for cdp_name, field in _COUNT_FIELDS.items():
        value = raw.get(cdp_name)
        mapped[field] = None if value is None else int(value)

    heap_bytes = raw.get("JSHeapUsedSize")
    mapped["js_heap_kb"] = None if heap_bytes is None else round(heap_bytes / 1024.0, 3)

    return mapped


def enable(cdp) -> None:
    """Start the Performance domain. Must be called BEFORE navigation.

    The counters only accumulate while the domain is enabled, so enabling it
    after ``load`` reports a near-zero main thread for the very page load we
    are trying to measure.
    """
    try:
        cdp.send("Performance.enable")
    except Exception:  # pragma: no cover - unsupported target
        pass


def collect_cdp_metrics(cdp) -> Dict[str, Optional[float]]:
    """Read DevTools main-thread counters for the current page.

    Assumes :func:`enable` ran before navigation. Best-effort: a CDP session
    that refuses the Performance domain yields all ``None`` rather than failing
    the whole measurement run.
    """
    try:
        payload = cdp.send("Performance.getMetrics")
    except Exception:  # pragma: no cover - defensive
        return {field: None for field in FIELDS}
    return map_metrics(payload)
