"""Grafana's column-major frames to :class:`FieldSnapshot`.

The wire shape is::

    results.<refId>.frames[].schema.fields[]  -> names and types
    results.<refId>.frames[].data.values[]    -> parallel arrays, one per field

Columns are matched by **name**, never by position, so a column added to a
panel does not silently shift every value one to the left. A field the schema
does not carry yields ``None`` rather than a zero.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from pydantic import ValidationError

from normalize.field import (
    AssetRow,
    CountryRow,
    DeviceRow,
    FieldSnapshot,
    FieldVitals,
    Frustration,
    InpBucketRow,
    PageTypeRow,
    SessionKpis,
    SessionRates,
    TimePoint,
    VitalsReading,
)

#: Columns shared by the ``vitals`` series and the ungrouped ``headline``
#: query - both feed a :class:`normalize.field.VitalsReading`.
_VITALS_KEYS = (
    "lcp_p75", "lcp_p95", "inp_p75", "inp_p95",
    "cls_p75", "cls_p95", "ttfb_p75", "plt_p75",
)


class ParseError(Exception):
    """A response this module cannot turn into a snapshot.

    Covers two distinct failures: a frame whose declared shape does not hold
    (see ``frame_rows``), and a value that parsed cleanly but a model
    rejected as out of range (see ``_construct``). Either way, the message
    is meant to name something a person running ``ingest field`` can act on
    - never a bare Pydantic trace pointing at ``by_pagetype.3.bounce_pct``
    with no mention of which panel or row sent it.
    """


def frame_rows(result: Any) -> List[Dict[str, Any]]:
    """Every frame of one panel, transposed to row dicts keyed by field name."""
    if not isinstance(result, dict):
        return []
    rows: List[Dict[str, Any]] = []
    for frame in result.get("frames") or []:
        fields = ((frame.get("schema") or {}).get("fields")) or []
        columns = (frame.get("data") or {}).get("values") or []
        if not fields or not columns:
            continue
        if len(fields) != len(columns):
            raise ParseError(
                f"Frame declares {len(fields)} fields but carries "
                f"{len(columns)} columns."
            )
        lengths = {len(column) for column in columns}
        if len(lengths) > 1:
            raise ParseError(f"Frame columns have differing lengths: {sorted(lengths)}")
        names = [str(field.get("name", "")) for field in fields]
        for index in range(lengths.pop() if lengths else 0):
            rows.append({name: columns[i][index] for i, name in enumerate(names)})
    return rows


def _num(row: Dict[str, Any], key: str) -> Optional[float]:
    """A numeric cell, or None. Never coerces a missing value to zero."""
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(row: Dict[str, Any], key: str) -> Optional[int]:
    value = _num(row, key)
    return None if value is None else int(round(value))


def _points(rows: Sequence[Dict[str, Any]]) -> List[TimePoint]:
    """Rows carrying a ``time`` column become the series, oldest first."""
    points: List[TimePoint] = []
    for row in rows:
        raw = row.get("time")
        if raw is None:
            continue
        # Grafana sends epoch milliseconds for time-typed fields.
        moment = datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
        points.append(TimePoint(
            time=moment,
            values={k: _num(row, k) for k in row if k != "time"},
        ))
    return sorted(points, key=lambda p: p.time)


def _last(points: Sequence[TimePoint], key: str) -> Optional[float]:
    """The newest non-null value for one column — the dashboard's reducer."""
    for point in reversed(points):
        value = point.values.get(key)
        if value is not None:
            return value
    return None


def _sum(points: Sequence[TimePoint], key: str) -> Optional[int]:
    """Total across buckets, or None when the column was never present.

    Distinct from ``sum(... or 0)``: a panel that never carried the column
    must not report a confident zero for something nobody measured. A zero
    session count and an unmeasured session count must never render
    identically, the same rule ``_num``/``_int`` already enforce per cell.
    """
    values = [p.values.get(key) for p in points]
    present = [v for v in values if v is not None]
    return int(sum(present)) if present else None


def _weighted_mean(
    points: Sequence[TimePoint], key: str, weight_key: str
) -> Optional[float]:
    """A true window figure for a rate column, weighted by its bucket weight.

    ``_last`` reports the newest bucket's value, which for a rate like
    ``bounce_pct`` is a point-in-time snapshot dressed up as a whole-window
    aggregate — the final bucket is always partial, and (for ``sessions``
    specifically) skewed toward sessions too young to have viewed a second
    page. This computes the window figure the dashboard's own SQL would:
    ``sum(value * weight) / sum(weight)`` over buckets that carry both.

    Distinct from ``_sum``'s all-or-nothing rule: a bucket missing only the
    rate, or only the weight, is skipped rather than poisoning every other
    bucket's contribution. None only when *no* bucket carries both — the
    same "never a confident number for something unmeasured" rule ``_sum``
    already enforces.
    """
    total = 0.0
    weight_total = 0.0
    for point in points:
        value = point.values.get(key)
        weight = point.values.get(weight_key)
        if value is None or not weight:
            continue
        total += value * weight
        weight_total += weight
    return (total / weight_total) if weight_total else None


def _construct(model, panel: str, index: Optional[int], fields: Dict[str, Any]):
    """Build one model, or raise a ``ParseError`` naming what caused it.

    A field can parse cleanly (``_num``/``_int`` never raise) and still be a
    value a model's bounds reject - a ``bounce_pct`` of 150 is not a parsing
    failure, it is an upstream bug. Pydantic already fails loudly on that;
    what it does not do on its own is say which panel and row sent it, and
    that is the one thing someone triaging a broken ``ingest field`` run
    needs. Clamping was considered and rejected: the query layer's
    percentage expressions are exact at their boundaries, so an overshoot
    is a real bug worth surfacing, not float noise worth hiding.
    """
    try:
        return model(**fields)
    except ValidationError as exc:
        where = panel if index is None else f"{panel}[{index}]"
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}="
            f"{err.get('input')!r} ({err['msg']})"
            for err in exc.errors()
        )
        raise ParseError(f"{where}: {details}") from exc


def build_snapshot(
    results: Dict[str, Any],
    *,
    project: str,
    hosts: Sequence[str],
    window_from: datetime,
    window_to: datetime,
    fetched_at: datetime,
) -> FieldSnapshot:
    """Assemble one snapshot from a full ``/api/ds/query`` results map."""
    session_points = _points(frame_rows(results.get("sessions")))
    vital_points = _points(frame_rows(results.get("vitals")))
    frustration_points = _points(frame_rows(results.get("frustration")))
    # Ungrouped: one row (or none) for the whole window, not a series - see
    # normalize.field.FieldVitals for why percentiles need this separate
    # query rather than being re-aggregated from `vital_points`.
    headline_rows = frame_rows(results.get("headline"))
    headline_row: Dict[str, Any] = headline_rows[0] if headline_rows else {}

    sessions = _construct(SessionKpis, "sessions", None, dict(
        # `window` is weighted by session_count, not `_last`: the newest
        # bucket is always partial and, for a session bucketed by its
        # *first* beacon, systematically bounce-heavy (visitors too recent
        # to have viewed a second page yet). See _weighted_mean's docstring
        # for why this weighted mean *is* the exact window figure, not an
        # approximation of it.
        window=_construct(SessionRates, "sessions.window", None, dict(
            bounce_pct=_weighted_mean(
                session_points, "bounce_pct", "session_count"
            ),
            conversion_pct=_weighted_mean(
                session_points, "conversion_pct", "session_count"
            ),
            avg_session_pages=_weighted_mean(
                session_points, "avg_session_pages", "session_count"
            ),
        )),
        latest=_construct(SessionRates, "sessions.latest", None, dict(
            bounce_pct=_last(session_points, "bounce_pct"),
            conversion_pct=_last(session_points, "conversion_pct"),
            avg_session_pages=_last(session_points, "avg_session_pages"),
        )),
        session_count=_sum(session_points, "session_count"),
        series=session_points,
    ))

    vitals = _construct(FieldVitals, "vitals", None, dict(
        # The true whole-window percentile, from the ungrouped `headline`
        # query - not re-aggregated from `vital_points` (a mean of p75s is
        # not the window's p75).
        window=_construct(VitalsReading, "headline", None, {
            key: _num(headline_row, key) for key in _VITALS_KEYS
        }),
        # The most recent interval bucket only.
        latest=_construct(VitalsReading, "vitals.latest", None, {
            key: _last(vital_points, key) for key in _VITALS_KEYS
        }),
        series=vital_points,
    ))

    frustration = _construct(Frustration, "frustration", None, dict(
        rage_clicks_total=_sum(frustration_points, "rage_clicks"),
        frustration_p75_window=_num(headline_row, "frustration_p75"),
        frustration_p75_latest=_last(frustration_points, "frustration_p75"),
        series=frustration_points,
    ))

    def segment(ref_id, model, key_field, key_name, extra=()):
        built = []
        for index, row in enumerate(frame_rows(results.get(ref_id))):
            label = row.get(key_field)
            if not label:
                continue
            fields = {
                key_name: str(label),
                "beacons": _int(row, "beacons"),
                "lcp_p75": _num(row, "lcp_p75"),
                "inp_p75": _num(row, "inp_p75"),
                "cls_p75": _num(row, "cls_p75"),
                "plt_p75": _num(row, "plt_p75"),
                "ttfb_p75": _num(row, "ttfb_p75"),
                "frustration_p75": _num(row, "frustration_p75"),
                "rage_clicks": _int(row, "rage_clicks"),
            }
            for name, reader in extra:
                fields[name] = reader(row)
            built.append(_construct(model, ref_id, index, fields))
        return built

    by_device = segment(
        "by_device", DeviceRow, "device", "device",
        extra=(("rage_pct", lambda r: _num(r, "rage_pct")),
               ("avg_session_pages", lambda r: _num(r, "avg_session_pages"))),
    )
    by_country = segment("by_country", CountryRow, "country", "country")
    by_pagetype = segment(
        "by_pagetype", PageTypeRow, "page_group", "page_group",
        extra=(("bounce_pct", lambda r: _num(r, "bounce_pct")),),
    )

    inp_buckets = [
        _construct(InpBucketRow, "inp_buckets", index, dict(
            bucket=str(row.get("bucket")),
            beacons=_int(row, "beacons"),
            avg_frustration=_num(row, "avg_frustration"),
            rage_session_pct=_num(row, "rage_session_pct"),
        ))
        for index, row in enumerate(frame_rows(results.get("inp_buckets")))
        if row.get("bucket")
    ]

    assets = [
        _construct(AssetRow, "assets", index, dict(
            asset_type=str(row.get("asset_type")),
            request_count=_int(row, "request_count"),
            avg_size_kb=_num(row, "avg_size_kb"),
            edge_ms=_num(row, "edge_ms"),
            origin_ms=_num(row, "origin_ms"),
            cache_hit_pct=_num(row, "cache_hit_pct"),
        ))
        for index, row in enumerate(frame_rows(results.get("assets")))
        if row.get("asset_type")
    ]

    return _construct(FieldSnapshot, "snapshot", None, dict(
        snapshot_id=f"{project}-{fetched_at.strftime('%Y%m%dT%H%M%SZ')}",
        project=project,
        hosts=list(hosts),
        window_from=window_from,
        window_to=window_to,
        fetched_at=fetched_at,
        sessions=sessions,
        vitals=vitals,
        frustration=frustration,
        by_device=by_device,
        by_country=by_country,
        by_pagetype=by_pagetype,
        inp_buckets=inp_buckets,
        assets=assets,
    ))
