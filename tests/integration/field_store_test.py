"""field_snapshots round-trip through SQLite."""
from datetime import datetime, timedelta, timezone

import pytest

from normalize.field import FieldSnapshot, PageTypeRow, SessionKpis, SessionRates
from store import sql

BASE = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


def _snap(snapshot_id="oakley-1", project="oakley", fetched_at=BASE, **kw):
    return FieldSnapshot(
        snapshot_id=snapshot_id,
        project=project,
        hosts=["www.oakley.com"],
        window_from=fetched_at - timedelta(days=7),
        window_to=fetched_at,
        fetched_at=fetched_at,
        **kw,
    )


@pytest.fixture
def conn():
    connection = sql.connect(":memory:")
    sql.init_schema(connection)
    yield connection
    connection.close()


def test_snapshot_round_trips_with_nested_rows(conn):
    original = _snap(
        sessions=SessionKpis(window=SessionRates(bounce_pct=44.5),
                             session_count=9100),
        by_pagetype=[PageTypeRow(page_group="Pdp", beacons=1200, lcp_p75=4100.0)],
    )
    sql.insert_snapshot(conn, original)

    loaded = sql.get_latest_snapshot(conn, "oakley")
    assert loaded.sessions.window.bounce_pct == 44.5
    assert loaded.page_row("Pdp").lcp_p75 == 4100.0
    assert loaded.window_from == original.window_from


def test_unmeasured_stays_none_across_the_round_trip(conn):
    sql.insert_snapshot(conn, _snap())
    loaded = sql.get_latest_snapshot(conn, "oakley")
    assert loaded.sessions.window.bounce_pct is None
    assert loaded.vitals.window.lcp_p75 is None


def test_latest_is_by_fetched_at_not_insertion_order(conn):
    sql.insert_snapshot(conn, _snap("old", fetched_at=BASE - timedelta(days=2)))
    sql.insert_snapshot(conn, _snap("new", fetched_at=BASE))
    sql.insert_snapshot(conn, _snap("mid", fetched_at=BASE - timedelta(days=1)))
    assert sql.get_latest_snapshot(conn, "oakley").snapshot_id == "new"


def test_latest_is_scoped_to_project(conn):
    sql.insert_snapshot(conn, _snap("o", project="oakley"))
    sql.insert_snapshot(conn, _snap("r", project="rayban",
                                    fetched_at=BASE + timedelta(days=1)))
    assert sql.get_latest_snapshot(conn, "oakley").snapshot_id == "o"


def test_missing_project_returns_none(conn):
    assert sql.get_latest_snapshot(conn, "nobody") is None


def test_duplicate_id_raises_unless_replacing(conn):
    sql.insert_snapshot(conn, _snap())
    with pytest.raises(sql.StoreError):
        sql.insert_snapshot(conn, _snap())
    sql.insert_snapshot(
        conn, _snap(sessions=SessionKpis(window=SessionRates(bounce_pct=1.0))),
        replace=True)
    assert sql.get_latest_snapshot(conn, "oakley").sessions.window.bounce_pct == 1.0


def test_list_snapshots_is_newest_first(conn):
    sql.insert_snapshot(conn, _snap("a", fetched_at=BASE - timedelta(days=1)))
    sql.insert_snapshot(conn, _snap("b", fetched_at=BASE))
    assert [s.snapshot_id for s in sql.list_snapshots(conn)] == ["b", "a"]
