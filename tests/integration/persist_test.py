"""Integration tests for ingest/persist.py — the campaign's persistence sink.

Everything here was a wiring gap rather than a missing capability: the run
store and the HAR scrubber were both written, tested and documented, and
nothing in the pipeline called either. These tests are the callers.
"""
from __future__ import annotations

import json

import pytest

from ingest.persist import RunPersister, run_filename
from normalize.schema import Run
from store import sql
from tests.integration.store_test import har_with_secrets, make_run


@pytest.fixture()
def conn():
    connection = sql.connect(":memory:")
    yield connection
    connection.close()


def written_har(tmp_path, extra_header=None):
    """A raw capture on disk, as the browser runner leaves it."""
    har = har_with_secrets()
    if extra_header is not None:
        har["log"]["entries"][0]["request"]["headers"].append(extra_header)
    source = tmp_path / "raw" / "capture.har"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(har), encoding="utf-8")
    return source


# --- the run store ---------------------------------------------------------- #


def test_a_persisted_run_is_in_the_run_store(tmp_path, conn):
    """The gap that left `list-runs` empty and every trend series `new`."""
    persist = RunPersister(output_dir=tmp_path / "processed", conn=conn)
    persist(make_run("run_a"))

    stored = sql.list_runs(conn)
    assert [r.run_id for r in stored] == ["run_a"]


def test_a_persisted_run_round_trips_through_the_store(tmp_path, conn):
    persist = RunPersister(output_dir=tmp_path / "processed", conn=conn)
    persist(make_run("run_a", lcp_ms=4820))

    assert sql.get_run(conn, "run_a").metrics.cwp.lcp_ms == 4820


def test_persisting_without_a_connection_still_writes_json(tmp_path):
    """`--no-store` and the dry paths must not require a database."""
    persist = RunPersister(output_dir=tmp_path / "processed")
    persist(make_run("run_a"))

    assert (tmp_path / "processed").glob("*.json")


def test_two_campaigns_accumulate_history_in_one_store(tmp_path, conn):
    """What the trend layer reads: the same series across campaigns."""
    persist = RunPersister(output_dir=tmp_path / "processed", conn=conn)
    persist(make_run("run_1", lcp_ms=6200, created_at="2026-01-08T14:30:00Z"))
    persist(make_run("run_2", lcp_ms=4800, created_at="2026-02-08T14:30:00Z"))

    history = sql.metric_history(conn, "lcp_ms", project="storefront")
    assert [row["value"] for row in history] == [6200, 4800]


# --- the JSON output -------------------------------------------------------- #


def test_the_run_json_is_written_where_analysis_reads_it(tmp_path, conn):
    persist = RunPersister(output_dir=tmp_path / "processed", conn=conn)
    persist(make_run("run_a"))

    written = list((tmp_path / "processed").glob("*.json"))
    assert len(written) == 1
    assert Run.model_validate(
        json.loads(written[0].read_text(encoding="utf-8"))
    ).run_id == "run_a"


def test_a_page_name_with_a_separator_cannot_escape_the_output_dir(tmp_path):
    """`checkout/step-2` is a legal page name in targets.yaml, not a path."""
    persist = RunPersister(output_dir=tmp_path / "processed")
    persist(make_run("run_a", page="checkout/step-2"))

    written = list((tmp_path / "processed").glob("*.json"))
    assert len(written) == 1
    assert "/" not in written[0].name and "\\" not in written[0].name


def test_run_filename_is_page_device_network():
    assert run_filename(make_run("run_a")) == "homepage__mid-mobile__slow-4g.json"


# --- artifacts -------------------------------------------------------------- #


def test_the_stored_har_has_no_credentials_in_it(tmp_path, conn):
    """SECURITY_PLAN 2.6: the store must never hold an unredacted HAR."""
    source = written_har(tmp_path)
    persist = RunPersister(
        output_dir=tmp_path / "processed", conn=conn, store_root=tmp_path / "store"
    )
    stored = persist(make_run("run_a", captures={"har": str(source)}))

    content = open(stored.captures.har, encoding="utf-8").read()
    assert "session=abc123" not in content
    assert "Bearer eyJhbGciOi" not in content
    assert "SECRET123" not in content


def test_a_configured_allowlist_token_is_redacted_from_the_stored_har(tmp_path):
    """The header name is project-specific, so the persister must pass it on."""
    source = written_har(
        tmp_path, extra_header={"name": "X-Akamai-Bot", "value": "super-secret"}
    )
    persist = RunPersister(
        output_dir=tmp_path / "processed",
        store_root=tmp_path / "store",
        extra_headers=["X-Akamai-Bot"],
    )
    stored = persist(make_run("run_a", captures={"har": str(source)}))

    assert "super-secret" not in open(stored.captures.har, encoding="utf-8").read()


def test_captures_are_repointed_at_the_scrubbed_copies(tmp_path, conn):
    """The report appendix reads `captures`; it must not read the raw HAR."""
    source = written_har(tmp_path)
    persist = RunPersister(
        output_dir=tmp_path / "processed", conn=conn, store_root=tmp_path / "store"
    )
    stored = persist(make_run("run_a", captures={"har": str(source)}))

    assert stored.captures.har != str(source)
    assert str(tmp_path / "store") in stored.captures.har


def test_the_raw_unscrubbed_har_does_not_survive(tmp_path):
    """Moved, not copied: leaving the original is leaving the credentials."""
    source = written_har(tmp_path)
    persist = RunPersister(
        output_dir=tmp_path / "processed", store_root=tmp_path / "store"
    )
    persist(make_run("run_a", captures={"har": str(source)}))

    assert not source.exists()


def test_the_stored_run_json_points_at_the_scrubbed_har(tmp_path):
    """What analysis re-reads must be the stored path, not the temp one."""
    source = written_har(tmp_path)
    persist = RunPersister(
        output_dir=tmp_path / "processed", store_root=tmp_path / "store"
    )
    persist(make_run("run_a", captures={"har": str(source)}))

    written = next((tmp_path / "processed").glob("*.json"))
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["captures"]["har"] != str(source)


def test_a_run_with_no_captures_persists_fine(tmp_path, conn):
    """A `--no-artifacts` campaign is valid and must not need a store root."""
    persist = RunPersister(
        output_dir=tmp_path / "processed", conn=conn, store_root=tmp_path / "store"
    )
    stored = persist(make_run("run_a"))

    assert stored.captures.har is None
    assert sql.list_runs(conn)[0].run_id == "run_a"


def test_without_a_store_root_captures_are_left_alone(tmp_path):
    source = written_har(tmp_path)
    persist = RunPersister(output_dir=tmp_path / "processed")
    stored = persist(make_run("run_a", captures={"har": str(source)}))

    assert stored.captures.har == str(source)
    assert source.exists()
