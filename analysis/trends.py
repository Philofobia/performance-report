"""Campaign-over-campaign comparison (PROJECT_SPEC §10 Phase 7).

``store.sql.metric_history`` was written in Phase 3 and documented as "the
trend-over-time input"; this is what consumes it. Everything here is pure —
history rows and the current campaign's runs in, series out — so the awkward
part (reading a database that may not exist) stays in one small function at
the bottom and the arithmetic stays testable without one.

Three rules shape the design:

**One series per (page, device, network, metric).** Comparing a desktop LCP
against a mobile LCP would manufacture regressions out of nothing — a campaign
that merely added a desktop condition would show every page improving.

**Series keys come from the current campaign, not from the store.** A
condition dropped from ``targets.yaml`` three campaigns ago does not reappear
as a trend; history only supplies earlier points for series that exist now.

**A small change is not a signal.** Emulated throttling varies run to run.
Without a dead band a 3% wobble reads as a regression every campaign, and the
section becomes noise the reader learns to skip.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

#: The metrics the comparison table already carries. All four are
#: lower-is-better, which is why ``compare`` needs no per-metric polarity.
TREND_METRICS: Tuple[str, ...] = ("lcp_ms", "cls", "inp_ms", "tbt_ms")

#: Metric → the ``Thresholds`` attribute holding its target. TBT is absent
#: because no TBT threshold is configured; its series carry no target and can
#: never report a crossing.
_TARGET_ATTR: Mapping[str, str] = {
    "lcp_ms": "lcp_good_ms",
    "cls": "cls_good",
    "inp_ms": "inp_good_ms",
}


@dataclass(frozen=True)
class TrendPoint:
    """One campaign's value for one metric under one condition.

    ``at`` stays the ISO string the store holds rather than a parsed
    ``datetime``: nothing computes on it, the query already returns rows
    oldest-first, and parsing would add a failure mode for no gain.
    """

    run_id: str
    value: float
    at: str


@dataclass(frozen=True)
class TrendSeries:
    page: str
    device: str
    network: str
    metric: str
    points: List[TrendPoint] = field(default_factory=list)
    direction: str = "new"
    delta_pct: Optional[float] = None
    target: Optional[float] = None
    crossed: Optional[str] = None


def compare(
    previous: float, latest: float, *, dead_band_pct: float
) -> Tuple[str, Optional[float]]:
    """``(direction, delta_pct)`` for the newest point against the one before.

    A zero previous value is reported flat with no delta rather than raising:
    a CLS of 0 that stays 0 has not regressed, and the alternative is a
    division error in the middle of a report.
    """
    if previous == 0:
        return "flat", None

    delta = (latest - previous) / previous * 100.0
    if abs(delta) < dead_band_pct:
        return "flat", delta
    return ("regressed" if delta > 0 else "improved"), delta


def crossed(
    previous: float, latest: float, target: Optional[float]
) -> Optional[str]:
    """Whether this campaign moved the metric across its target.

    Reported separately from direction because they answer different
    questions: direction is which way it is moving, crossing is whether it
    broke the budget. A metric can improve substantially and still fail.
    """
    if target is None:
        return None
    was_pass, is_pass = previous <= target, latest <= target
    if was_pass and not is_pass:
        return "into_fail"
    if is_pass and not was_pass:
        return "into_pass"
    return None


def _target_for(metric: str, thresholds: Any) -> Optional[float]:
    attr = _TARGET_ATTR.get(metric)
    return None if attr is None else float(getattr(thresholds, attr))


def _current_points(runs: Sequence[Any]) -> Dict[Tuple[str, str, str, str], TrendPoint]:
    """The campaign's own points, keyed by (page, device, network, metric).

    A metric the current run did not measure yields no key, so no series is
    reported for it — there is no trend to state *now*, whatever history holds.
    """
    points: Dict[Tuple[str, str, str, str], TrendPoint] = {}
    for run in runs:
        for metric in TREND_METRICS:
            value = getattr(run.metrics.cwp, metric, None)
            if value is None:
                continue
            key = (run.page.name, run.condition.device,
                   run.condition.network, metric)
            points[key] = TrendPoint(
                run_id=run.run_id, value=float(value),
                at=run.meta.created_at.isoformat(),
            )
    return points


def build_series(
    runs: Sequence[Any],
    *,
    history: Sequence[Mapping[str, Any]],
    thresholds: Any,
    dead_band_pct: float,
    window: int,
) -> Dict[str, List[TrendSeries]]:
    """Trend series for a campaign, keyed by page name.

    ``history`` is whatever ``load_history`` returned, oldest first. Its order
    is preserved rather than re-sorted: the query already ordered by
    ``created_at``, and two campaigns recorded in the same second would sort
    arbitrarily if re-sorted here.
    """
    current = _current_points(runs)

    earlier: Dict[Tuple[str, str, str, str], List[TrendPoint]] = {}
    for row in history:
        value = row.get("value")
        if value is None:
            continue
        key = (row["page_name"], row["device"], row["network"], row["metric"])
        if key not in current:
            # A condition or metric this campaign did not measure.
            continue
        if row["run_id"] == current[key].run_id:
            # `analyze --from-store` reads the very runs this query returns.
            continue
        earlier.setdefault(key, []).append(TrendPoint(
            run_id=row["run_id"], value=float(value),
            at=str(row.get("created_at", "")),
        ))

    by_page: Dict[str, List[TrendSeries]] = {}
    for key, point in current.items():
        page, device, network, metric = key
        points = earlier.get(key, []) + [point]
        # Truncate *after* appending, so the newest point is always kept and
        # the comparison pair survives any window size.
        points = points[-window:]

        target = _target_for(metric, thresholds)
        if len(points) < 2:
            series = TrendSeries(page=page, device=device, network=network,
                                 metric=metric, points=points, target=target)
        else:
            previous, latest = points[-2].value, points[-1].value
            direction, delta = compare(previous, latest,
                                       dead_band_pct=dead_band_pct)
            series = TrendSeries(
                page=page, device=device, network=network, metric=metric,
                points=points, direction=direction, delta_pct=delta,
                target=target, crossed=crossed(previous, latest, target),
            )
        by_page.setdefault(page, []).append(series)

    for page in by_page:
        by_page[page].sort(
            key=lambda s: (s.device, s.network, TREND_METRICS.index(s.metric))
        )
    return by_page


def load_history(
    db_path: Union[str, Path], *, project: str
) -> List[Dict[str, Any]]:
    """Every trended metric's history for one project, oldest first.

    Returns ``[]`` for a missing, empty or unreadable store. Analysis never
    fails because history is unavailable — the same rule that governs the LLM
    degradation path. The first campaign a user ever runs must still produce a
    complete report, and it does: every series is ``new``.
    """
    path = Path(db_path)
    if not path.is_file():
        return []

    from store import sql

    rows: List[Dict[str, Any]] = []
    try:
        conn = sql.connect(path)
        try:
            for metric in TREND_METRICS:
                for row in sql.metric_history(conn, metric, project=project):
                    rows.append({**row, "metric": metric})
        finally:
            conn.close()
    except (sql.StoreError, sqlite3.Error, OSError):
        return []
    return rows
