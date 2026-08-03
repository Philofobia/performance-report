"""Chart builders: plain data in, SVG text out (PROJECT_SPEC §6.1).

Every builder is a pure function. It takes numbers and strings — never a
``Report`` — so a chart can be exercised with three floats and no fixture
document, and so nothing in this module can accidentally start deriving values
the analysis layer is supposed to own.

**SVG, not PNG.** Vector output survives print DPI, the HTML stays a single
self-contained file with no asset paths for Chromium to resolve, and — the
reason that matters here — SVG is text, so tests assert what a chart *shows*
rather than that an opaque blob exists.

**Determinism is not free.** matplotlib randomises SVG element ids per process
and stamps a creation date into the file. Both are defeated below
(``svg.hashsalt`` and ``metadata={"Date": None}``), along with explicit figure
sizes and a bundled font, so output does not depend on the machine's fonts or
DPI. Without these three controls §6.2's "same data, same report" is false.

A builder that cannot honestly draw its chart returns :data:`NO_CHART` (the
empty string) rather than an empty or misleading figure. The template renders
an explicit empty state in the same slot — the section is never omitted.
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # no display backend, ever — must precede pyplot

import matplotlib.pyplot as plt  # noqa: E402 - deliberately after use("Agg")
from io import StringIO  # noqa: E402
from typing import Mapping, Optional, Sequence  # noqa: E402

from report import palette  # noqa: E402

# Pinned so SVG element ids are stable across processes. Any constant works;
# what matters is that it never changes, or every previously rendered report
# stops matching a fresh render of the same data.
HASH_SALT = "performance-report-v1"
matplotlib.rcParams["svg.hashsalt"] = HASH_SALT
matplotlib.rcParams["font.family"] = "DejaVu Sans"  # ships with matplotlib
# "none" keeps labels as real <text> elements. The alternative, "path",
# converts them to vector outlines: portable, but the text then exists only as
# geometry - unselectable in the PDF, invisible to search, and impossible for
# a test to assert on. Since glyph *positions* are computed from matplotlib's
# bundled font metrics either way, the SVG stays byte-identical run to run;
# only the final glyph shapes depend on the renderer's font, and style.css
# pins a fallback stack for that.
matplotlib.rcParams["svg.fonttype"] = "none"

# Falsy on purpose: `{% if chart %}` in the template reads naturally.
NO_CHART = ""

# Human labels for the metrics a gauge row shows.
_CWV_LABELS = (("lcp_ms", "LCP", "ms"), ("cls", "CLS", ""), ("inp_ms", "INP", "ms"))

_GAUGE_THRESHOLD_KEYS = {
    "lcp_ms": ("lcp_good_ms", "lcp_fail_ms"),
    "cls": ("cls_good", "cls_fail"),
    "inp_ms": ("inp_good_ms", "inp_fail_ms"),
}


def _fmt(value: float, unit: str) -> str:
    """Render a metric value the way the report states it elsewhere."""
    if unit == "ms":
        return f"{value:.0f} ms"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def to_svg(fig) -> str:
    """Serialise a figure to inline-ready SVG and release it.

    The XML prologue and DOCTYPE are stripped because the output is embedded
    inside an HTML document, where a second XML declaration is invalid. The
    figure is closed here rather than by callers: matplotlib keeps every open
    figure alive globally, and a report with six charts per page would leak
    them all.
    """
    buffer = StringIO()
    fig.savefig(
        buffer,
        format="svg",
        metadata={"Date": None},  # otherwise every render differs
        bbox_inches="tight",
        transparent=True,
    )
    plt.close(fig)
    svg = buffer.getvalue()
    start = svg.find("<svg")
    return svg[start:] if start != -1 else svg


def cwv_gauges(
    metrics: Mapping[str, Optional[float]], thresholds: Mapping[str, float]
) -> str:
    """One horizontal gauge per Core Web Vital, coloured against its target.

    A gauge rather than a bare number because the question the reader has is
    not "what is the LCP" but "how far past the target is it" — so the bar is
    drawn against the failing threshold, with the target marked.
    """
    present = [
        (key, label, unit)
        for key, label, unit in _CWV_LABELS
        if metrics.get(key) is not None
    ]
    if not present:
        return NO_CHART

    fig, axes = plt.subplots(
        len(present), 1, figsize=(6.2, 0.85 * len(present)), squeeze=False
    )
    for row, (key, label, unit) in enumerate(present):
        axis = axes[row][0]
        value = float(metrics[key])
        verdict = palette.classify(key, value, thresholds)
        colour = palette.colour_for(verdict)

        good_key, fail_key = _GAUGE_THRESHOLD_KEYS[key]
        good = thresholds.get(good_key)
        fail = thresholds.get(fail_key)
        # Scale so the whole gauge is readable even when the value is far past
        # the failing threshold.
        span = max(value, fail or value, good or value) * 1.15 or 1.0

        axis.barh([0], [value], color=colour, height=0.5)
        if good is not None:
            axis.axvline(good, color=palette.MUTED, linewidth=1, linestyle="--")
        axis.set_xlim(0, span)
        axis.set_ylim(-0.5, 0.5)
        axis.set_yticks([])
        axis.set_xticks([])
        for side in ("top", "right", "bottom", "left"):
            axis.spines[side].set_visible(False)
        axis.text(
            0, 0.42, label, color=palette.INK, fontsize=9, fontweight="bold",
            va="bottom",
        )
        axis.text(
            span, 0.42, _fmt(value, unit), color=colour, fontsize=9,
            fontweight="bold", va="bottom", ha="right",
        )
        if good is not None:
            axis.text(
                good, -0.5, f"target {_fmt(good, unit)}", color=palette.MUTED,
                fontsize=7, va="top", ha="center",
            )

    fig.tight_layout(h_pad=1.2)
    return to_svg(fig)
