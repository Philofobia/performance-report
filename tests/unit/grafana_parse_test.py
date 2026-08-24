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
