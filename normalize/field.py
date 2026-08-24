"""Pydantic canonical field-data object (design 2026-08-24 §4).

``FieldSnapshot`` sits *beside* :class:`normalize.schema.Run`, never inside it.
``Run`` is validated per page x device x network and ``_require_cwv_for_automated``
enforces a lab CWV trio; field data is session-scoped and brand-wide with no
``condition`` at all, so folding it in would make both that validator and the
``runs`` table describe something they do not hold.

Every metric is ``Optional`` with no default coercion, following the rule
``MainThreadMetrics`` documents: a panel that returned nothing stays ``None``.
A zero bounce rate and an unmeasured bounce rate must never render identically.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

#: Percentages arriving from ClickHouse are already 0..100.
Pct = Optional[float]


class TimePoint(BaseModel):
    """One bucket of a time series. ``values`` is keyed by column name."""

    time: datetime
    values: Dict[str, Optional[float]] = Field(default_factory=dict)


class SessionKpis(BaseModel):
    """Whole-window session aggregates, plus the series behind them."""

    bounce_pct: Pct = Field(default=None, ge=0, le=100)
    conversion_pct: Pct = Field(default=None, ge=0, le=100)
    avg_session_pages: Optional[float] = Field(default=None, ge=0)
    session_count: Optional[int] = Field(default=None, ge=0)
    series: List[TimePoint] = Field(default_factory=list)


class FieldVitals(BaseModel):
    """Real-user Core Web Vitals. Compare against the lab numbers, not to them."""

    lcp_p75: Optional[float] = Field(default=None, ge=0)
    lcp_p95: Optional[float] = Field(default=None, ge=0)
    inp_p75: Optional[float] = Field(default=None, ge=0)
    inp_p95: Optional[float] = Field(default=None, ge=0)
    #: Already divided by 1000 at query time — mPulse stores CLS x1000.
    cls_p75: Optional[float] = Field(default=None, ge=0)
    cls_p95: Optional[float] = Field(default=None, ge=0)
    ttfb_p75: Optional[float] = Field(default=None, ge=0)
    plt_p75: Optional[float] = Field(default=None, ge=0)
    series: List[TimePoint] = Field(default_factory=list)


class Frustration(BaseModel):
    rage_clicks_total: Optional[int] = Field(default=None, ge=0)
    frustration_p75: Optional[float] = Field(default=None, ge=0, le=100)
    series: List[TimePoint] = Field(default_factory=list)


class _SegmentRow(BaseModel):
    """Columns every breakdown row shares."""

    beacons: Optional[int] = Field(default=None, ge=0)
    lcp_p75: Optional[float] = Field(default=None, ge=0)
    inp_p75: Optional[float] = Field(default=None, ge=0)
    cls_p75: Optional[float] = Field(default=None, ge=0)
    plt_p75: Optional[float] = Field(default=None, ge=0)
    ttfb_p75: Optional[float] = Field(default=None, ge=0)
    frustration_p75: Optional[float] = Field(default=None, ge=0, le=100)
    rage_clicks: Optional[int] = Field(default=None, ge=0)


class DeviceRow(_SegmentRow):
    device: str = Field(min_length=1)
    rage_pct: Pct = Field(default=None, ge=0, le=100)
    avg_session_pages: Optional[float] = Field(default=None, ge=0)


class CountryRow(_SegmentRow):
    country: str = Field(min_length=1)


class PageTypeRow(_SegmentRow):
    page_group: str = Field(min_length=1)
    #: Sessions that *entered* on this page group and viewed one page. Not the
    #: same quantity as the site-wide bounce rate, which is per session
    #: regardless of entry point - see the query in ingest/grafana/queries.py.
    bounce_pct: Pct = Field(default=None, ge=0, le=100)


class InpBucketRow(BaseModel):
    bucket: str = Field(min_length=1)
    beacons: Optional[int] = Field(default=None, ge=0)
    avg_frustration: Optional[float] = Field(default=None, ge=0, le=100)
    rage_session_pct: Pct = Field(default=None, ge=0, le=100)


class AssetRow(BaseModel):
    asset_type: str = Field(min_length=1)
    request_count: Optional[int] = Field(default=None, ge=0)
    avg_size_kb: Optional[float] = Field(default=None, ge=0)
    edge_ms: Optional[float] = Field(default=None, ge=0)
    origin_ms: Optional[float] = Field(default=None, ge=0)
    cache_hit_pct: Pct = Field(default=None, ge=0, le=100)


class FieldSnapshot(BaseModel):
    """One fetch of field data for a project, over one absolute window."""

    snapshot_id: str = Field(min_length=1)
    project: str = Field(min_length=1)
    hosts: List[str] = Field(min_length=1)
    #: Absolute UTC, resolved at fetch time. "now-7d" is not reproducible, and
    #: a report re-rendered next month must still state the week it describes.
    window_from: datetime
    window_to: datetime
    fetched_at: datetime

    sessions: SessionKpis = Field(default_factory=SessionKpis)
    vitals: FieldVitals = Field(default_factory=FieldVitals)
    frustration: Frustration = Field(default_factory=Frustration)
    by_device: List[DeviceRow] = Field(default_factory=list)
    by_country: List[CountryRow] = Field(default_factory=list)
    by_pagetype: List[PageTypeRow] = Field(default_factory=list)
    inp_buckets: List[InpBucketRow] = Field(default_factory=list)
    assets: List[AssetRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _window_ordered(self) -> "FieldSnapshot":
        if self.window_to < self.window_from:
            raise ValueError("window_to must not precede window_from")
        return self

    def page_row(self, group: str) -> Optional[PageTypeRow]:
        """The breakdown row for one ``pageGroupName``, or None if absent."""
        for row in self.by_pagetype:
            if row.page_group == group:
                return row
        return None


#: Grafana relative-window suffixes, in seconds.
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def window_delta(window: str) -> timedelta:
    """``"7d"`` -> 7 days.

    Lives here rather than in the ingestion layer because both sides need it:
    ingestion resolves ``now-7d`` to the absolute window it stores, and the
    report decides whether a stored snapshot has gone stale. ``normalize`` is
    the only package both already import.
    """
    text = window.strip().lower()
    if len(text) < 2 or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise ValueError(
            f"Unsupported window {window!r}. Use a number followed by "
            "m, h, d or w - for example 7d."
        )
    return timedelta(seconds=int(text[:-1]) * _UNITS[text[-1]])
