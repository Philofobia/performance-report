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
    TimePoint,
)


class ParseError(Exception):
    """A response this module cannot turn into a snapshot."""


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

    sessions = SessionKpis(
        bounce_pct=_last(session_points, "bounce_pct"),
        conversion_pct=_last(session_points, "conversion_pct"),
        avg_session_pages=_last(session_points, "avg_session_pages"),
        session_count=(
            int(sum(p.values.get("session_count") or 0 for p in session_points))
            if session_points else None
        ),
        series=session_points,
    )

    vitals = FieldVitals(
        **{
            key: _last(vital_points, key)
            for key in ("lcp_p75", "lcp_p95", "inp_p75", "inp_p95",
                        "cls_p75", "cls_p95", "ttfb_p75", "plt_p75")
        },
        series=vital_points,
    )

    frustration = Frustration(
        rage_clicks_total=(
            int(sum(p.values.get("rage_clicks") or 0 for p in frustration_points))
            if frustration_points else None
        ),
        frustration_p75=_last(frustration_points, "frustration_p75"),
        series=frustration_points,
    )

    def segment(ref_id, model, key_field, key_name, extra=()):
        built = []
        for row in frame_rows(results.get(ref_id)):
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
            built.append(model(**fields))
        return built

    return FieldSnapshot(
        snapshot_id=f"{project}-{fetched_at.strftime('%Y%m%dT%H%M%SZ')}",
        project=project,
        hosts=list(hosts),
        window_from=window_from,
        window_to=window_to,
        fetched_at=fetched_at,
        sessions=sessions,
        vitals=vitals,
        frustration=frustration,
        by_device=segment(
            "by_device", DeviceRow, "device", "device",
            extra=(("rage_pct", lambda r: _num(r, "rage_pct")),
                   ("avg_session_pages", lambda r: _num(r, "avg_session_pages"))),
        ),
        by_country=segment("by_country", CountryRow, "country", "country"),
        by_pagetype=segment(
            "by_pagetype", PageTypeRow, "page_group", "page_group",
            extra=(("bounce_pct", lambda r: _num(r, "bounce_pct")),),
        ),
        inp_buckets=[
            InpBucketRow(
                bucket=str(row.get("bucket")),
                beacons=_int(row, "beacons"),
                avg_frustration=_num(row, "avg_frustration"),
                rage_session_pct=_num(row, "rage_session_pct"),
            )
            for row in frame_rows(results.get("inp_buckets"))
            if row.get("bucket")
        ],
        assets=[
            AssetRow(
                asset_type=str(row.get("asset_type")),
                request_count=_int(row, "request_count"),
                avg_size_kb=_num(row, "avg_size_kb"),
                edge_ms=_num(row, "edge_ms"),
                origin_ms=_num(row, "origin_ms"),
                cache_hit_pct=_num(row, "cache_hit_pct"),
            )
            for row in frame_rows(results.get("assets"))
            if row.get("asset_type")
        ],
    )
