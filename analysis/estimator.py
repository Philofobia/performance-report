"""Rule-based improvement projections (PROJECT_SPEC §6 section 6, §11).

This module is deliberately **pure**: no store, no config, no LLM, no file
I/O. That is not tidiness — it is the mitigation for §11's "LLM hallucinated
improvement magnitudes" risk. The model picks *which* playbook applies; the
numbers come from here, and this code cannot see a single word the model
wrote.

Magnitudes originate in playbook front matter, which ``rag/knowledge.py``
already parses and carries through
``Chunk.metadata -> Document.metadata -> SearchHit.metadata``:

    expected_lcp_reduction_pct: 15, 40
    expected_cls_reduction_abs: 0.05, 0.15
    effort: low

Percentages are stored as fractions internally so the arithmetic never has to
remember which unit it is in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

# How much a second, third, ... fix on the *same* metric is discounted. Stacked
# optimisations overlap: the second image fix cannot re-win bytes the first one
# already removed.
DECAY = 0.8

# No stack of recommendations may claim more than this share of a metric.
MAX_TOTAL_REDUCTION = 0.70

_VALID_EFFORTS = ("low", "medium", "high")

# Front-matter stems -> canonical metric names on the run object.
_METRIC_ALIASES = {
    "lcp": "lcp_ms",
    "fcp": "fcp_ms",
    "ttfb": "ttfb_ms",
    "inp": "inp_ms",
    "tbt": "tbt_ms",
    "cls": "cls",
    "transfer": "total_transfer_kb",
    "page_weight": "total_transfer_kb",
}

_RANGE_KEY = re.compile(r"^expected_(?P<stem>[a-z_]+?)_reduction_(?P<unit>pct|abs)$")


@dataclass(frozen=True)
class ImpactRange:
    """One playbook's expected effect on one metric.

    ``low``/``high`` are fractions (0.15 == 15%) when ``absolute`` is False,
    and metric-native units (0.05 CLS) when it is True.
    """

    metric: str
    low: float
    high: float
    absolute: bool = False


@dataclass(frozen=True)
class Candidate:
    """A recommendation as the estimator sees it: a source and its metadata.

    Deliberately not the richer ``Recommendation`` from ``findings.py`` — the
    estimator must not depend on anything that has touched model output.
    """

    source: str
    metadata: Mapping[str, Any]


def _as_bounds(value: Any) -> Optional[tuple]:
    """Coerce a front-matter value into an ordered ``(low, high)`` pair.

    ``rag.knowledge._coerce`` turns "15, 40" into ``[15, 40]`` and a bare "30"
    into ``30``, so both shapes arrive here.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (float(value), float(value))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            low, high = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
        return (min(low, high), max(low, high))
    return None


def parse_impact_ranges(metadata: Mapping[str, Any]) -> List[ImpactRange]:
    """Read every ``expected_<metric>_reduction_<pct|abs>`` key.

    Sorted by metric so a playbook always yields its ranges in the same order —
    the first link in the determinism chain (§6.2).
    """
    found: List[ImpactRange] = []
    for key, raw in metadata.items():
        match = _RANGE_KEY.match(str(key))
        if not match:
            continue
        metric = _METRIC_ALIASES.get(match.group("stem"))
        if metric is None:
            continue
        bounds = _as_bounds(raw)
        if bounds is None:
            continue
        low, high = bounds
        absolute = match.group("unit") == "abs"
        if not absolute:
            low, high = low / 100.0, high / 100.0
        found.append(ImpactRange(metric=metric, low=low, high=high, absolute=absolute))
    return sorted(found, key=lambda r: r.metric)


def effort_of(metadata: Mapping[str, Any]) -> str:
    """Playbook effort level, or ``"unknown"`` when absent or unrecognised."""
    value = str(metadata.get("effort", "")).strip().lower()
    return value if value in _VALID_EFFORTS else "unknown"


@dataclass(frozen=True)
class Projection:
    """One recommendation's projected effect on one metric.

    ``after_low`` is the conservative end and drives the headline and the
    chart; ``after_high`` is the optimistic edge of the playbook's own band.
    Under-promising and landing beats a midpoint that misses.
    """

    metric: str
    before: float
    after_low: float
    after_high: float
    reduction_pct: float
    source: str


def _apply(value: float, amount: float, absolute: bool, floor: float) -> float:
    """Apply one reduction to ``value``, clamped at ``floor`` and at zero."""
    reduced = value - amount if absolute else value * (1.0 - amount)
    return max(0.0, floor, reduced)


def project(
    candidates: Sequence[Candidate],
    metrics: Mapping[str, Optional[float]],
) -> List[Projection]:
    """Project each candidate's effect, stacking per metric with decay.

    Candidates affecting the same metric are applied in descending order of
    their low bound (ties broken by source, so the order never depends on how
    the caller happened to sort them). Each subsequent fix on that metric is
    discounted by ``DECAY`` and applied to what the previous one left.

    A candidate with no usable range for any *measured* metric contributes
    nothing — the caller still lists it, with magnitude "unknown", per the
    system prompt's rule 3.
    """
    # Bucket (source, range) pairs by metric, keeping only measured metrics.
    buckets: Dict[str, List[tuple]] = {}
    for candidate in candidates:
        for rng in parse_impact_ranges(candidate.metadata):
            measured = metrics.get(rng.metric)
            if measured is None:
                continue
            buckets.setdefault(rng.metric, []).append((candidate.source, rng))

    projections: List[Projection] = []
    for metric in sorted(buckets):
        baseline = float(metrics[metric])
        floor = baseline * (1.0 - MAX_TOTAL_REDUCTION)
        entries = sorted(buckets[metric], key=lambda pair: (-pair[1].low, pair[0]))

        current_low = baseline
        current_high = baseline
        for position, (source, rng) in enumerate(entries):
            decay = DECAY ** position
            before = current_low
            after_low = _apply(current_low, rng.low * decay, rng.absolute, floor)
            after_high = _apply(current_high, rng.high * decay, rng.absolute, floor)
            reduction = 0.0 if before <= 0 else (before - after_low) / before
            projections.append(
                Projection(
                    metric=metric,
                    before=before,
                    after_low=after_low,
                    after_high=after_high,
                    reduction_pct=reduction,
                    source=source,
                )
            )
            current_low, current_high = after_low, after_high

    return projections


def aggregate(
    projections: Sequence[Projection],
    metrics: Mapping[str, Optional[float]],
) -> Dict[str, Projection]:
    """Collapse per-metric chains into one before/after each (§6 chart)."""
    out: Dict[str, Projection] = {}
    for metric in sorted({p.metric for p in projections}):
        chain = [p for p in projections if p.metric == metric]
        baseline = float(metrics[metric])
        last = chain[-1]
        reduction = 0.0 if baseline <= 0 else (baseline - last.after_low) / baseline
        out[metric] = Projection(
            metric=metric,
            before=baseline,
            after_low=last.after_low,
            after_high=last.after_high,
            reduction_pct=reduction,
            source="aggregate",
        )
    return out


def by_source(projections: Sequence[Projection]) -> Dict[str, List[Projection]]:
    """Group projections by the playbook that produced them."""
    out: Dict[str, List[Projection]] = {}
    for projection in projections:
        out.setdefault(projection.source, []).append(projection)
    return out


def rank_key(source: str, title: str, projections: Sequence[Projection]) -> tuple:
    """Deterministic sort key for recommendations (§7.1).

    Percentage, not absolute delta: 600ms and 0.2 CLS are not comparable, but
    "cuts this metric by 20%" is. Recommendations with no projection sort last
    rather than being dropped.
    """
    best = max((p.reduction_pct for p in projections), default=0.0)
    return (-best, source, title)
