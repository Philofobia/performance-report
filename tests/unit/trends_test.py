"""Unit tests for analysis/trends.py — campaign-over-campaign comparison.

Two properties carry the weight here. **The dead band**: emulated throttling
varies run to run, so a small delta must read as flat or the section becomes
noise the reader learns to skip. **Series keys come from the current
campaign**: history may hold conditions that are no longer tested, and a report
is about the campaign in front of you.
"""
from __future__ import annotations

import sqlite3

import pytest

from analysis import trends
from config.load import Thresholds
from normalize.schema import Run
from store import sql

THRESHOLDS = Thresholds()


def a_run(run_id="run_2", *, page="homepage", device="mid-mobile",
          network="slow-4g", lcp=4820.0, cls=0.12, inp=210.0, tbt=620.0,
          created_at="2026-08-04T00:00:00+00:00"):
    return Run.model_validate({
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com/"},
        "page": {"name": page, "url": f"https://example.com/{page}"},
        "condition": {"device": device, "network": network,
                      "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": created_at, "source": "manual", "runner": "test"},
        "metrics": {"cwp": {"lcp_ms": lcp, "cls": cls, "inp_ms": inp,
                            "tbt_ms": tbt}},
    })


def history_row(run_id, metric, value, *, page="homepage", device="mid-mobile",
                network="slow-4g", created_at="2026-07-01T00:00:00+00:00"):
    return {"run_id": run_id, "page_name": page, "device": device,
            "network": network, "created_at": created_at, "value": value,
            "metric": metric}


def series_for(result, metric, *, page="homepage"):
    return next(s for s in result[page] if s.metric == metric)


def build(runs, history=(), **kwargs):
    return trends.build_series(
        runs, history=list(history), thresholds=THRESHOLDS,
        dead_band_pct=kwargs.pop("dead_band_pct", 5.0),
        window=kwargs.pop("window", 5),
    )


# --- direction --------------------------------------------------------------


def test_a_rise_beyond_the_dead_band_is_a_regression():
    assert trends.compare(4820.0, 5210.0, dead_band_pct=5.0)[0] == "regressed"


def test_a_fall_beyond_the_dead_band_is_an_improvement():
    assert trends.compare(5940.0, 4820.0, dead_band_pct=5.0)[0] == "improved"


@pytest.mark.parametrize("latest", [6200.0 * 1.049, 6200.0 * 0.951])
def test_a_change_inside_the_dead_band_is_flat(latest):
    assert trends.compare(6200.0, latest, dead_band_pct=5.0)[0] == "flat"


def test_the_dead_band_edge_belongs_to_the_signal_not_to_flat():
    # Exactly 5% is a direction; only strictly-smaller changes are flat.
    assert trends.compare(100.0, 105.0, dead_band_pct=5.0)[0] == "regressed"


def test_the_dead_band_is_configurable():
    assert trends.compare(100.0, 108.0, dead_band_pct=10.0)[0] == "flat"
    assert trends.compare(100.0, 108.0, dead_band_pct=5.0)[0] == "regressed"


def test_the_delta_carries_sign_and_magnitude():
    _direction, delta = trends.compare(5000.0, 4000.0, dead_band_pct=5.0)
    assert delta == pytest.approx(-20.0)


def test_a_flat_change_still_reports_its_delta():
    _direction, delta = trends.compare(100.0, 102.0, dead_band_pct=5.0)
    assert delta == pytest.approx(2.0)


def test_a_zero_previous_value_is_flat_with_no_delta():
    # A CLS of 0 that stays 0 has not regressed, and must not divide by zero.
    assert trends.compare(0.0, 0.0, dead_band_pct=5.0) == ("flat", None)
    assert trends.compare(0.0, 0.3, dead_band_pct=5.0) == ("flat", None)


# --- target crossing --------------------------------------------------------


def test_crossing_above_the_target_is_into_fail():
    assert trends.crossed(2300.0, 2700.0, 2500.0) == "into_fail"


def test_crossing_below_the_target_is_into_pass():
    assert trends.crossed(2700.0, 2300.0, 2500.0) == "into_pass"


def test_staying_on_one_side_of_the_target_is_not_a_crossing():
    assert trends.crossed(2700.0, 2600.0, 2500.0) is None
    assert trends.crossed(2300.0, 2400.0, 2500.0) is None


def test_a_metric_with_no_configured_target_never_crosses():
    assert trends.crossed(700.0, 900.0, None) is None


# --- series assembly --------------------------------------------------------


def test_a_first_campaign_has_one_point_and_reads_as_new():
    result = build([a_run()])
    lcp = series_for(result, "lcp_ms")
    assert lcp.direction == "new"
    assert lcp.delta_pct is None
    assert len(lcp.points) == 1


def test_history_supplies_the_earlier_points():
    result = build([a_run(lcp=4820.0)],
                   [history_row("run_1", "lcp_ms", 6200.0)])
    lcp = series_for(result, "lcp_ms")
    assert [p.value for p in lcp.points] == [6200.0, 4820.0]
    assert lcp.direction == "improved"
    assert lcp.delta_pct == pytest.approx(-22.26, abs=0.01)


def test_the_current_campaign_is_always_the_newest_point():
    result = build([a_run("run_2", lcp=4820.0)],
                   [history_row("run_1", "lcp_ms", 6200.0)])
    assert series_for(result, "lcp_ms").points[-1].run_id == "run_2"


def test_a_run_already_in_the_store_is_not_counted_twice():
    # `analyze --from-store` reads the very runs the history query returns.
    result = build([a_run("run_2", lcp=4820.0)], [
        history_row("run_1", "lcp_ms", 6200.0),
        history_row("run_2", "lcp_ms", 4820.0),
    ])
    lcp = series_for(result, "lcp_ms")
    assert [p.run_id for p in lcp.points] == ["run_1", "run_2"]


def test_every_trended_metric_gets_its_own_series():
    result = build([a_run()])
    assert {s.metric for s in result["homepage"]} == set(trends.TREND_METRICS)


def test_conditions_are_never_merged_into_one_series():
    # A desktop LCP compared against a mobile LCP would manufacture a
    # regression out of nothing.
    result = build(
        [a_run("run_m", device="mid-mobile", network="slow-4g", lcp=4820.0),
         a_run("run_d", device="desktop", network="fast-3g", lcp=1930.0)],
        [history_row("run_1", "lcp_ms", 6200.0,
                     device="mid-mobile", network="slow-4g")],
    )
    mobile = next(s for s in result["homepage"]
                  if s.device == "mid-mobile" and s.metric == "lcp_ms")
    desktop = next(s for s in result["homepage"]
                   if s.device == "desktop" and s.metric == "lcp_ms")
    assert [p.value for p in mobile.points] == [6200.0, 4820.0]
    assert [p.value for p in desktop.points] == [1930.0]
    assert desktop.direction == "new"


def test_history_for_a_condition_no_longer_tested_is_dropped():
    # The report is about the campaign in front of you.
    result = build([a_run(device="mid-mobile")],
                   [history_row("run_1", "lcp_ms", 1930.0, device="desktop")])
    assert all(s.device == "mid-mobile" for s in result["homepage"])


def test_history_for_another_page_does_not_leak_in():
    result = build([a_run(page="homepage")],
                   [history_row("run_1", "lcp_ms", 6200.0, page="plp")])
    assert set(result) == {"homepage"}
    assert len(series_for(result, "lcp_ms").points) == 1


def test_each_page_gets_its_own_entry():
    result = build([a_run("run_h", page="homepage"), a_run("run_p", page="plp")])
    assert set(result) == {"homepage", "plp"}


def test_the_window_keeps_the_newest_points():
    older = [history_row(f"run_{i}", "lcp_ms", float(6000 + i)) for i in range(6)]
    result = build([a_run("run_now", lcp=4820.0)], older, window=3)
    lcp = series_for(result, "lcp_ms")
    assert [p.run_id for p in lcp.points] == ["run_4", "run_5", "run_now"]


def test_the_direction_survives_truncation():
    # Truncation must not change which point the newest is compared against.
    older = [history_row(f"run_{i}", "lcp_ms", float(6000 + i)) for i in range(6)]
    windowed = build([a_run("run_now", lcp=4820.0)], older, window=3)
    full = build([a_run("run_now", lcp=4820.0)], older, window=10)
    assert (series_for(windowed, "lcp_ms").direction
            == series_for(full, "lcp_ms").direction)


def test_a_metric_the_current_run_did_not_measure_has_no_series():
    # No current point means no trend to report *now*, even if history has one.
    result = build([a_run(inp=None)], [history_row("run_1", "inp_ms", 480.0)])
    assert all(s.metric != "inp_ms" for s in result["homepage"])


def test_history_rows_with_no_value_are_dropped():
    result = build([a_run(lcp=4820.0)],
                   [history_row("run_1", "lcp_ms", None)])
    assert len(series_for(result, "lcp_ms").points) == 1


def test_series_carry_the_configured_target():
    result = build([a_run()])
    assert series_for(result, "lcp_ms").target == float(THRESHOLDS.lcp_good_ms)
    assert series_for(result, "cls").target == THRESHOLDS.cls_good


def test_tbt_has_no_target_because_none_is_configured():
    result = build([a_run()])
    assert series_for(result, "tbt_ms").target is None


def test_a_series_reports_crossing_its_target():
    result = build([a_run(lcp=2700.0)], [history_row("run_1", "lcp_ms", 2300.0)])
    assert series_for(result, "lcp_ms").crossed == "into_fail"


def test_ordering_is_explicit_so_the_render_stays_deterministic():
    result = build([a_run("run_d", device="desktop", network="fast-3g"),
                    a_run("run_m", device="mid-mobile", network="slow-4g")])
    ordered = [(s.device, s.metric) for s in result["homepage"]]
    assert ordered == sorted(
        ordered,
        key=lambda pair: (pair[0], trends.TREND_METRICS.index(pair[1])),
    )
    # Metric order follows TREND_METRICS, not the alphabet.
    metrics = [s.metric for s in result["homepage"] if s.device == "desktop"]
    assert metrics == list(trends.TREND_METRICS)


# --- loading history --------------------------------------------------------


def test_load_history_reads_every_trended_metric(tmp_path):
    db = tmp_path / "runs.sqlite"
    conn = sql.connect(db)
    sql.insert_run(conn, a_run("run_1", lcp=6200.0))
    conn.close()

    rows = trends.load_history(db, project="storefront")
    assert {row["metric"] for row in rows} == set(trends.TREND_METRICS)
    lcp = [r for r in rows if r["metric"] == "lcp_ms"]
    assert lcp[0]["value"] == 6200.0
    assert lcp[0]["run_id"] == "run_1"


def test_load_history_is_scoped_to_the_project(tmp_path):
    db = tmp_path / "runs.sqlite"
    conn = sql.connect(db)
    sql.insert_run(conn, a_run("run_1"))
    conn.close()

    assert trends.load_history(db, project="somebody-else") == []


def test_a_missing_store_yields_no_history_rather_than_an_error(tmp_path):
    # Analysis must never fail because history is unavailable.
    assert trends.load_history(tmp_path / "nope.sqlite", project="storefront") == []


def test_an_unreadable_store_yields_no_history(tmp_path, monkeypatch):
    db = tmp_path / "runs.sqlite"
    sql.connect(db).close()

    def boom(*_args, **_kwargs):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(sql, "metric_history", boom)
    assert trends.load_history(db, project="storefront") == []


def test_a_store_with_no_history_still_produces_a_complete_report(tmp_path):
    result = build([a_run()], trends.load_history(tmp_path / "nope.sqlite",
                                                  project="storefront"))
    assert all(s.direction == "new" for s in result["homepage"])
