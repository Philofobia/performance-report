"""FieldSnapshot validation — the canonical field-data object."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from normalize.field import FieldSnapshot, PageTypeRow, SessionKpis


def _snapshot(**kwargs) -> FieldSnapshot:
    base = dict(
        snapshot_id="oakley-20260824T100000Z",
        project="oakley",
        hosts=["www.oakley.com"],
        window_from=datetime(2026, 8, 17, tzinfo=timezone.utc),
        window_to=datetime(2026, 8, 24, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    base.update(kwargs)
    return FieldSnapshot(**base)


def test_minimal_snapshot_validates_with_every_metric_none():
    snap = _snapshot()
    assert snap.sessions.bounce_pct is None
    assert snap.vitals.lcp_p75 is None
    assert snap.by_device == []


def test_unmeasured_is_not_zero():
    """A panel that returned nothing must not read as a measured zero."""
    snap = _snapshot()
    assert snap.sessions.bounce_pct is None
    assert snap.sessions.session_count is None
    # The distinction the whole model turns on: None formats as an em dash,
    # 0 formats as a number a reader would act on.
    assert snap.sessions.bounce_pct != 0


def test_percentages_are_bounded():
    with pytest.raises(ValidationError):
        _snapshot(sessions=SessionKpis(bounce_pct=101.0))
    with pytest.raises(ValidationError):
        _snapshot(sessions=SessionKpis(conversion_pct=-1.0))


def test_window_must_not_be_inverted():
    with pytest.raises(ValidationError):
        _snapshot(
            window_from=datetime(2026, 8, 24, tzinfo=timezone.utc),
            window_to=datetime(2026, 8, 17, tzinfo=timezone.utc),
        )


def test_page_row_looks_up_by_group():
    snap = _snapshot(by_pagetype=[
        PageTypeRow(page_group="Pdp", beacons=900, lcp_p75=4100.0),
        PageTypeRow(page_group="Home", beacons=500, lcp_p75=2200.0),
    ])
    assert snap.page_row("Pdp").lcp_p75 == 4100.0
    assert snap.page_row("Payment") is None


def test_hosts_must_not_be_empty():
    with pytest.raises(ValidationError):
        _snapshot(hosts=[])


@pytest.mark.parametrize("text,seconds", [
    ("7d", 604800), ("24h", 86400), ("30m", 1800), ("2w", 1209600),
])
def test_window_delta_parses_grafana_syntax(text, seconds):
    from normalize.field import window_delta

    assert window_delta(text).total_seconds() == seconds


@pytest.mark.parametrize("bad", ["", "d", "7", "7y", "seven days", "-7d", "7 d"])
def test_window_delta_rejects_what_it_cannot_parse(bad):
    from normalize.field import window_delta

    with pytest.raises(ValueError):
        window_delta(bad)
