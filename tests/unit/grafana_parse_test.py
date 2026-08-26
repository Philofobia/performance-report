"""Frames to models. Mapping is by field NAME, never by position."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingest.grafana.parse import ParseError, build_snapshot, frame_rows, _weighted_mean
from normalize.field import TimePoint

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
    # The `latest` scalar is the most recent bucket, matching the
    # dashboard's "lastNotNull" reducer. The fixture carries no `headline`
    # panel, so `window` stays None rather than being approximated from
    # the series.
    assert snap.vitals.latest.lcp_p75 == 4100.0
    assert snap.vitals.window.lcp_p75 is None
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


# --------------------------------------------------------------------------- #
# _weighted_mean — a true window rate, not the last (partial, bounce-heavy)
# bucket (whole-branch review item 1)
# --------------------------------------------------------------------------- #
def _tp(**values):
    return TimePoint(time=datetime(2026, 8, 24, tzinfo=timezone.utc), values=values)


def test_weighted_mean_of_uneven_buckets_is_the_true_window_figure():
    """[40, 41, 92]% bounce with counts [1000, 1000, 50] must land near the
    session-weighted 41.8%, not the last bucket's 92.0% - the reproduction
    from the branch review: the final bucket is always partial, and a
    session bucketed by its first beacon skews the newest bucket toward
    visitors too recent to have viewed a second page.
    """
    points = [
        _tp(bounce_pct=40.0, session_count=1000.0),
        _tp(bounce_pct=41.0, session_count=1000.0),
        _tp(bounce_pct=92.0, session_count=50.0),
    ]
    result = _weighted_mean(points, "bounce_pct", "session_count")
    assert result == pytest.approx(41.756, abs=0.01)
    assert result != pytest.approx(92.0)


def test_weighted_mean_skips_a_bucket_with_a_missing_weight():
    """A bucket carrying the rate but not the weight must not corrupt the
    result - it is dropped, not treated as weight zero or weight one.
    """
    points = [
        _tp(bounce_pct=40.0, session_count=1000.0),
        _tp(bounce_pct=99.0),  # no session_count in this bucket
    ]
    result = _weighted_mean(points, "bounce_pct", "session_count")
    assert result == pytest.approx(40.0)


def test_weighted_mean_skips_a_bucket_with_a_zero_weight():
    points = [
        _tp(bounce_pct=40.0, session_count=1000.0),
        _tp(bounce_pct=99.0, session_count=0.0),
    ]
    result = _weighted_mean(points, "bounce_pct", "session_count")
    assert result == pytest.approx(40.0)


def test_weighted_mean_is_none_when_no_bucket_carries_both():
    points = [_tp(session_count=1000.0), _tp(bounce_pct=41.0)]
    assert _weighted_mean(points, "bounce_pct", "session_count") is None


def test_weighted_mean_is_none_for_no_points():
    assert _weighted_mean([], "bounce_pct", "session_count") is None


def _sessions_panel_with_counts(times, bounce_pcts, counts, conv_pcts=None,
                                 avg_pages=None):
    fields = [
        {"name": "time", "type": "time"},
        {"name": "bounce_pct", "type": "number"},
        {"name": "session_count", "type": "number"},
    ]
    values = [times, bounce_pcts, counts]
    if conv_pcts is not None:
        fields.append({"name": "conversion_pct", "type": "number"})
        values.append(conv_pcts)
    if avg_pages is not None:
        fields.append({"name": "avg_session_pages", "type": "number"})
        values.append(avg_pages)
    return {"sessions": {"frames": [{
        "schema": {"fields": fields}, "data": {"values": values},
    }]}}


def test_snapshot_bounce_pct_carries_both_the_window_and_latest_readings():
    """End-to-end through build_snapshot: `window` and `latest` must never
    collapse into one number. [40, 41, 92]% bounce with counts
    [1000, 1000, 50] gives a window figure near 41.8% and a latest-bucket
    figure of exactly 92.0% - the dashboard's own "lastNotNull" reducer.
    """
    results = _sessions_panel_with_counts(
        times=[1787529600000, 1787616000000, 1787702400000],
        bounce_pcts=[40.0, 41.0, 92.0],
        counts=[1000, 1000, 50],
    )
    snap = _snapshot(results)
    assert snap.sessions.window.bounce_pct == pytest.approx(41.756, abs=0.01)
    assert snap.sessions.latest.bounce_pct == pytest.approx(92.0)
    assert snap.sessions.session_count == 2050


def test_headline_query_populates_window_and_series_populates_latest():
    """The `headline` refId (ungrouped, whole-window) feeds `vitals.window`;
    the newest bucket of the `vitals` (bucketed) series feeds
    `vitals.latest`. They must come from the two different queries, not
    both be derived from the series.
    """
    results = {
        "vitals": {"frames": [{
            "schema": {"fields": [
                {"name": "time", "type": "time"},
                {"name": "lcp_p75", "type": "number"},
            ]},
            "data": {"values": [
                [1787529600000, 1787616000000],
                [3000.0, 4100.0],
            ]},
        }]},
        "headline": {"frames": [{
            "schema": {"fields": [{"name": "lcp_p75", "type": "number"}]},
            "data": {"values": [[3550.0]]},
        }]},
    }
    snap = _snapshot(results)
    assert snap.vitals.window.lcp_p75 == 3550.0
    assert snap.vitals.latest.lcp_p75 == 4100.0
