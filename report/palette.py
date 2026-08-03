"""Colour decisions, made once (PROJECT_SPEC §6.2).

§6.2 requires "fixed palettes (pass = green, warn = amber, fail = red)
computed from thresholds". Both halves matter: the colours are fixed, and the
*classification* comes from the configured thresholds rather than from a
number a chart author felt was bad. If any chart picked its own colour, two
runs of the same campaign could disagree about what red means, and the report
would stop being comparable — which is the whole point of the project.

Deliberately free of matplotlib: a badge in the HTML and a bar in a chart must
draw the same conclusion from the same value, so this module is importable by
both without dragging a plotting library into the template layer.
"""
from __future__ import annotations

from typing import Mapping, Optional, Tuple

# Verdict tokens. Chosen for print: each stays distinguishable in greyscale and
# under the common red-green colour vision deficiencies, where hue alone is not
# a safe carrier of meaning.
PASS = "#1a7f4b"
WARN = "#b26a00"
FAIL = "#b3261e"
UNKNOWN = "#6b7280"

# Document ink and chart furniture.
INK = "#1a1d21"
MUTED = "#6b7280"
GRID = "#d8dce1"

# Categorical series (resource types in the donut). Ordered, so the same type
# gets the same colour in every report.
CATEGORICAL: Tuple[str, ...] = (
    "#2f5d8f",
    "#7a9e3f",
    "#b26a00",
    "#8a5fa8",
    "#3f8f8a",
    "#a8556b",
    "#6b7280",
)

_VERDICT_COLOURS = {
    "pass": PASS,
    "warn": WARN,
    "fail": FAIL,
    "unknown": UNKNOWN,
}

# metric -> (good threshold key, fail threshold key or None).
# A metric with no fail key can never be worse than "warn": the config simply
# does not express a failing level for it, and inventing one here would be
# exactly the kind of local decision this module exists to prevent.
_METRIC_THRESHOLDS = {
    "lcp_ms": ("lcp_good_ms", "lcp_fail_ms"),
    "cls": ("cls_good", "cls_fail"),
    "inp_ms": ("inp_good_ms", "inp_fail_ms"),
    "fcp_ms": ("fcp_good_ms", None),
    "ttfb_ms": ("ttfb_good_ms", None),
}


def classify(
    metric: str, value: Optional[float], thresholds: Mapping[str, float]
) -> str:
    """Verdict for one metric value against the configured thresholds.

    Boundaries are inclusive on the good side: a metric sitting exactly on its
    target has met the target. Anything else would report a page as degraded
    for hitting the number it was asked to hit.
    """
    keys = _METRIC_THRESHOLDS.get(metric)
    if keys is None or value is None:
        return "unknown"

    good_key, fail_key = keys
    good = thresholds.get(good_key)
    if good is None:
        return "unknown"
    if value <= good:
        return "pass"

    fail = thresholds.get(fail_key) if fail_key else None
    if fail is not None and value > fail:
        return "fail"
    return "warn"


def colour_for(verdict: str) -> str:
    """Hex colour for a verdict; unrecognised verdicts render as unknown."""
    return _VERDICT_COLOURS.get(verdict, UNKNOWN)


def categorical_for(index: int) -> str:
    """Stable categorical colour, wrapping rather than raising."""
    return CATEGORICAL[index % len(CATEGORICAL)]
