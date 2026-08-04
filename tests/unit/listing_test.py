"""Unit tests for store/listing.py — the `list-runs` command.

The property worth pinning is that an absent metric prints as an em dash and
never as ``0``. A run with no INP entry is the documented no-handler case
(README, "How measurement works"); rendering it as zero would turn a missing
measurement into the best possible score.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from normalize.schema import Run
from store import sql
from store.listing import EMPTY, format_run_table, main


def a_run(run_id="run_1", *, page="homepage", device="mid-mobile",
          network="slow-4g", lcp=4820.0, cls=0.12, inp=210.0):
    return Run.model_validate({
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com/"},
        "page": {"name": page, "url": f"https://example.com/{page}"},
        "condition": {"device": device, "network": network,
                      "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
                 "source": "manual", "runner": "test"},
        "metrics": {"cwp": {"lcp_ms": lcp, "cls": cls, "inp_ms": inp}},
    })


def rows(table):
    return table.splitlines()


# --- formatting -------------------------------------------------------------


def test_the_header_names_every_column():
    header = rows(format_run_table([a_run()]))[0]
    for column in ("RUN ID", "PAGE", "DEVICE", "NETWORK", "LCP", "CLS", "INP"):
        assert column in header


def test_a_run_renders_its_identity_and_metrics():
    row = rows(format_run_table([a_run()]))[1]
    assert "run_1" in row
    assert "homepage" in row
    assert "mid-mobile" in row
    assert "slow-4g" in row
    assert "4820" in row
    assert "0.12" in row
    assert "210" in row


def test_a_missing_metric_is_an_em_dash_not_a_zero():
    row = rows(format_run_table([a_run(inp=None)]))[1]
    assert EMPTY in row
    assert "0" not in row.split()[-1]


def test_columns_stay_aligned_when_a_run_id_is_long():
    table = format_run_table([a_run("run_1"), a_run("a" * 40, page="plp")])
    header, first, second = rows(table)
    assert first.index("homepage") == second.index("plp")
    assert header.index("PAGE") == first.index("homepage")


def test_an_empty_run_list_still_prints_the_header():
    assert rows(format_run_table([]))[0].startswith("RUN ID")


def test_rows_keep_the_order_they_were_given():
    table = format_run_table([a_run("newest"), a_run("older")])
    assert rows(table)[1].startswith("newest")


# --- the command ------------------------------------------------------------


@pytest.fixture()
def populated_db(tmp_path):
    path = tmp_path / "runs.sqlite"
    conn = sql.connect(path)
    sql.insert_runs(conn, [
        a_run("run_home", page="homepage"),
        a_run("run_plp", page="plp", device="desktop", network="fast-3g"),
    ])
    conn.close()
    return path


def test_listing_prints_every_stored_run(populated_db, capsys):
    assert main(["--db", str(populated_db)]) == 0
    out = capsys.readouterr().out
    assert "run_home" in out
    assert "run_plp" in out


def test_pages_filter_narrows_the_listing(populated_db, capsys):
    assert main(["--db", str(populated_db), "--pages", "plp"]) == 0
    out = capsys.readouterr().out
    assert "run_plp" in out
    assert "run_home" not in out


def test_several_page_names_are_all_listed(populated_db, capsys):
    assert main(["--db", str(populated_db), "--pages", "plp,homepage"]) == 0
    out = capsys.readouterr().out
    assert "run_plp" in out
    assert "run_home" in out


def test_device_and_network_filters_reach_the_query(populated_db, capsys):
    assert main(["--db", str(populated_db), "--device", "desktop",
                 "--network", "fast-3g"]) == 0
    out = capsys.readouterr().out
    assert "run_plp" in out
    assert "run_home" not in out


def test_limit_caps_the_number_of_rows(populated_db, capsys):
    assert main(["--db", str(populated_db), "--limit", "1"]) == 0
    assert len(rows(capsys.readouterr().out.strip())) == 2  # header + 1


def test_an_empty_store_is_reported_not_failed(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    sql.connect(path).close()
    assert main(["--db", str(path)]) == 0
    assert "no runs stored" in capsys.readouterr().out


def test_a_missing_database_fails_rather_than_reading_as_empty(tmp_path, capsys):
    # connect() would happily create it; a typo'd --db must not look like
    # "you have no runs".
    missing = tmp_path / "nope.sqlite"
    assert main(["--db", str(missing)]) == 1
    assert "nope.sqlite" in capsys.readouterr().err
    assert not missing.exists()
