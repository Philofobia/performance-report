"""Frames to models. Mapping is by field NAME, never by position."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingest.grafana.parse import ParseError, build_snapshot, frame_rows

FIXTURE = Path(__file__).parent.parent / "fixtures" / "grafana_response.json"


def _results():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["results"]


def _snapshot(results=None):
    return build_snapshot(
        results if results is not None else _results(),
        project="oakley",
        hosts=["www.oakley.com"],
        window_from=datetime(2026, 8, 17, tzinfo=timezone.utc),
        window_to=datetime(2026, 8, 24, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )


def test_frame_rows_transposes_column_major_values():
    rows = frame_rows(_results()["by_pagetype"])
    assert rows[0] == {
        "page_group": "Pdp", "beacons": 1200, "lcp_p75": 4100.0,
        "frustration_p75": 42.0, "rage_clicks": 310, "bounce_pct": 61.5,
    }


def test_empty_frames_yield_no_rows():
    assert frame_rows(_results()["assets"]) == []


def test_missing_panel_yields_no_rows():
    assert frame_rows(None) == []


def test_columns_are_matched_by_name_not_position():
    """A column added to a panel must not shift every value one to the left."""
    results = _results()
    frame = results["by_pagetype"]["frames"][0]
    frame["schema"]["fields"].insert(
        1, {"name": "unexpected_new_column", "type": "string"}
    )
    frame["data"]["values"].insert(1, ["x", "y"])

    snap = _snapshot(results)
    assert snap.page_row("Pdp").lcp_p75 == 4100.0
    assert snap.page_row("Pdp").beacons == 1200


def test_absent_column_becomes_none_not_zero():
    snap = _snapshot()
    assert snap.page_row("Pdp").inp_p75 is None


def test_timeseries_becomes_series_and_latest_scalar():
    snap = _snapshot()
    assert len(snap.vitals.series) == 2
    # The scalar is the most recent bucket, matching the dashboard's
    # "lastNotNull" reducer.
    assert snap.vitals.lcp_p75 == 4100.0
    assert snap.vitals.series[0].time.year == 2026


def test_pagetype_rows_populate_the_join_lookup():
    snap = _snapshot()
    assert snap.page_row("Pdp").bounce_pct == 61.5
    assert snap.page_row("Home").rage_clicks == 40


def test_snapshot_id_is_derived_from_project_and_fetch_time():
    snap = _snapshot()
    assert snap.snapshot_id == "oakley-20260824T000000Z"


def test_frame_with_mismatched_column_lengths_raises():
    results = _results()
    results["by_pagetype"]["frames"][0]["data"]["values"][1] = [1200]
    with pytest.raises(ParseError):
        _snapshot(results)


def _sessions_panel(fields, values):
    """A minimal one-frame ``sessions`` panel, for aggregate-sum tests."""
    return {"sessions": {"frames": [{
        "schema": {"fields": fields},
        "data": {"values": values},
    }]}}


def _frustration_panel(fields, values):
    """A minimal one-frame ``frustration`` panel, for aggregate-sum tests."""
    return {"frustration": {"frames": [{
        "schema": {"fields": fields},
        "data": {"values": values},
    }]}}


def test_session_count_is_none_when_column_never_carried():
    """Buckets exist, but no bucket ever carries session_count - stays None.

    ``sum(... or 0)`` would report a confident zero sessions here, which
    reads as "nobody visited" rather than "we didn't measure this".
    """
    results = _sessions_panel(
        fields=[{"name": "time", "type": "time"}, {"name": "bounce_pct", "type": "number"}],
        values=[[1787529600000, 1787616000000], [10.0, 12.0]],
    )
    snap = _snapshot(results)
    assert snap.sessions.session_count is None


def test_session_count_sums_only_the_present_values():
    """A null bucket is excluded from the sum, not treated as a zero."""
    results = _sessions_panel(
        fields=[{"name": "time", "type": "time"}, {"name": "session_count", "type": "number"}],
        values=[
            [1787529600000, 1787616000000, 1787702400000],
            [100, None, 50],
        ],
    )
    snap = _snapshot(results)
    assert snap.sessions.session_count == 150


def test_rage_clicks_total_is_none_when_column_never_carried():
    results = _frustration_panel(
        fields=[{"name": "time", "type": "time"}, {"name": "frustration_p75", "type": "number"}],
        values=[[1787529600000, 1787616000000], [20.0, 25.0]],
    )
    snap = _snapshot(results)
    assert snap.frustration.rage_clicks_total is None


def test_rage_clicks_total_sums_only_the_present_values():
    results = _frustration_panel(
        fields=[{"name": "time", "type": "time"}, {"name": "rage_clicks", "type": "number"}],
        values=[
            [1787529600000, 1787616000000, 1787702400000],
            [5, None, 7],
        ],
    )
    snap = _snapshot(results)
    assert snap.frustration.rage_clicks_total == 12


def test_out_of_range_value_raises_parse_error_naming_field_and_value():
    """A datasource sending an impossible value fails loudly, but actionably.

    Bare Pydantic ValidationError names the field but not which panel or row
    produced it. ParseError must wrap it and say all three.
    """
    results = _results()
    # bounce_pct is column index 5 in the by_pagetype fixture frame; 150 is
    # outside PageTypeRow's 0..100 bound.
    results["by_pagetype"]["frames"][0]["data"]["values"][5] = [150.0, 38.0]
    with pytest.raises(ParseError) as exc_info:
        _snapshot(results)
    message = str(exc_info.value)
    assert "bounce_pct" in message
    assert "150" in message
