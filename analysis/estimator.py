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
