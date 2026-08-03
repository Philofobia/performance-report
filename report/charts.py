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


LCP_PHASES_CAPTION = "Derived from paint milestones, not from LCP sub-part timings."

# Human labels for metrics that appear as chart axis rows.
_METRIC_LABELS = {
    "lcp_ms": "LCP", "cls": "CLS", "inp_ms": "INP",
    "fcp_ms": "FCP", "ttfb_ms": "TTFB", "tbt_ms": "TBT",
    "total_transfer_kb": "Page weight",
}


def _shorten(name: str, limit: int = 34) -> str:
    """Keep the tail of a URL path — the filename carries the information."""
    if len(name) <= limit:
        return name
    return "…" + name[-(limit - 1):]


def _bare_axis(axis) -> None:
    """Strip chart furniture that adds ink without adding information."""
    for side in ("top", "right", "bottom", "left"):
        axis.spines[side].set_visible(False)
    axis.tick_params(length=0, labelsize=8, colors=palette.MUTED)


def resource_bars(resources: Sequence[Mapping], *, limit: int = 8) -> str:
    """Heaviest resources by transfer size — the evidence for "where"."""
    ranked = sorted(
        (r for r in resources if (r.get("transfer_kb") or 0) > 0),
        key=lambda r: (-(r.get("transfer_kb") or 0.0), r.get("name", "")),
    )[:limit]
    if not ranked:
        return NO_CHART

    labels = [_shorten(str(r.get("name", ""))) for r in ranked]
    values = [float(r.get("transfer_kb") or 0.0) for r in ranked]
    types = [str(r.get("type", "other")) for r in ranked]
    ordered_types = sorted(set(types))
    colours = [palette.categorical_for(ordered_types.index(t)) for t in types]

    fig, axis = plt.subplots(figsize=(6.2, 0.36 * len(ranked) + 0.6))
    positions = list(range(len(ranked)))
    axis.barh(positions, values, color=colours, height=0.62)
    axis.set_yticks(positions)
    axis.set_yticklabels(labels)
    axis.invert_yaxis()  # heaviest at the top, where the eye starts
    axis.set_xlabel("KB transferred", fontsize=8, color=palette.MUTED)
    axis.xaxis.grid(True, color=palette.GRID, linewidth=0.6)
    axis.set_axisbelow(True)
    _bare_axis(axis)
    fig.tight_layout()
    return to_svg(fig)


def request_type_donut(totals: Mapping[str, float]) -> str:
    """Share of transferred bytes by resource type."""
    present = {k: float(v) for k, v in totals.items() if (v or 0) > 0}
    if not present:
        return NO_CHART

    keys = sorted(present, key=lambda k: (-present[k], k))
    values = [present[k] for k in keys]
    colours = [palette.categorical_for(i) for i in range(len(keys))]

    fig, axis = plt.subplots(figsize=(4.0, 3.0))
    axis.pie(
        values,
        labels=keys,
        colors=colours,
        startangle=90,           # fixed, or slices rotate between renders
        counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 1},
        textprops={"fontsize": 8, "color": palette.INK},
        autopct=lambda pct: f"{pct:.0f}%" if pct >= 6 else "",
    )
    axis.set_aspect("equal")
    fig.tight_layout()
    return to_svg(fig)


def lcp_phases(cwp: Mapping[str, Optional[float]]) -> str:
    """LCP split into three phases derived from the paint milestones.

    The ingestion layer never captured the LCP entry's own sub-part timings,
    so these phases are derived: server (TTFB), render-blocking (FCP − TTFB)
    and LCP element (LCP − FCP). The chart says so in a visible caption — a
    coarser breakdown honestly labelled beats a precise-looking fiction.

    Any missing input, or a negative phase (FCP can post-date LCP on an odd
    run), yields no chart rather than a misleading one.
    """
    ttfb = cwp.get("ttfb_ms")
    fcp = cwp.get("fcp_ms")
    lcp = cwp.get("lcp_ms")
    if ttfb is None or fcp is None or lcp is None:
        return NO_CHART

    phases = [
        ("Server", float(ttfb)),
        ("Render-blocking", float(fcp) - float(ttfb)),
        ("LCP element", float(lcp) - float(fcp)),
    ]
    if any(width < 0 for _, width in phases):
        return NO_CHART

    fig, axis = plt.subplots(figsize=(6.2, 1.5))
    left = 0.0
    for index, (label, width) in enumerate(phases):
        axis.barh([0], [width], left=[left], height=0.5,
                  color=palette.categorical_for(index), label=label)
        left += width

    axis.set_xlim(0, max(left, 1.0))
    axis.set_ylim(-0.6, 0.6)
    axis.set_yticks([])
    axis.set_xlabel("ms", fontsize=8, color=palette.MUTED)
    _bare_axis(axis)
    axis.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.35), ncol=3,
        frameon=False, fontsize=8,
    )
    axis.text(
        0, -1.15, LCP_PHASES_CAPTION, fontsize=7, color=palette.MUTED,
        transform=axis.get_yaxis_transform(), va="top",
    )
    fig.tight_layout()
    return to_svg(fig)


def projection_bars(projections: Mapping[str, Mapping]) -> str:
    """Measured value against the conservatively projected value."""
    if not projections:
        return NO_CHART

    keys = sorted(projections)
    labels = [_METRIC_LABELS.get(k, k) for k in keys]
    before = [float(projections[k]["before"]) for k in keys]
    after = [float(projections[k]["after_low"]) for k in keys]

    fig, axis = plt.subplots(figsize=(6.2, 0.62 * len(keys) + 0.9))
    positions = list(range(len(keys)))
    height = 0.34
    axis.barh([p + height / 2 for p in positions], before, height=height,
              color=palette.MUTED, label="measured")
    axis.barh([p - height / 2 for p in positions], after, height=height,
              color=palette.PASS, label="projected")
    axis.set_yticks(positions)
    axis.set_yticklabels(labels)
    axis.invert_yaxis()
    axis.xaxis.grid(True, color=palette.GRID, linewidth=0.6)
    axis.set_axisbelow(True)
    _bare_axis(axis)
    axis.legend(loc="lower right", frameon=False, fontsize=8)
    fig.tight_layout()
    return to_svg(fig)


def comparison_heat(rows: Sequence[Mapping]) -> str:
    """Verdict per page × condition, as a compact coloured grid."""
    if not rows:
        return NO_CHART

    labels = [f"{r.get('page')} — {r.get('device')}/{r.get('network')}"
              for r in rows]
    colours = [palette.colour_for(str(r.get("verdict", "unknown"))) for r in rows]
    values = [float(r.get("lcp_ms") or 0.0) for r in rows]

    fig, axis = plt.subplots(figsize=(6.2, 0.38 * len(rows) + 0.7))
    positions = list(range(len(rows)))
    axis.barh(positions, values, color=colours, height=0.6)
    axis.set_yticks(positions)
    axis.set_yticklabels(labels)
    axis.invert_yaxis()
    axis.set_xlabel("LCP (ms)", fontsize=8, color=palette.MUTED)
    axis.xaxis.grid(True, color=palette.GRID, linewidth=0.6)
    axis.set_axisbelow(True)
    _bare_axis(axis)
    fig.tight_layout()
    return to_svg(fig)
