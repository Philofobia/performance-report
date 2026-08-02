"""Integration tests for store/sql.py and store/artifacts.py.

SQLite runs in-memory or under ``tmp_path``; artifacts are written to
``tmp_path``. No network, no external services (TESTING_PLAN.md §3).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from normalize.schema import Run
from store import artifacts as art
from store import sql


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def make_run(
    run_id: str = "run_20260108_1430_ab12",
    *,
    page: str = "homepage",
    device: str = "mid-mobile",
    network: str = "slow-4g",
    lcp_ms: float = 6200,
    created_at: str = "2026-01-08T14:30:00Z",
    source: str = "automated",
    captures: dict | None = None,
) -> Run:
    return Run.model_validate({
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com"},
        "page": {"name": page, "url": f"https://example.com/{page}"},
        "condition": {"device": device, "network": network, "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": created_at, "source": source, "runner": "cli-1.0"},
        "problem": {"description": "LCP spikes on 3G", "keywords": ["lcp", "3g"]},
        "metrics": {
            "cwp": {"lcp_ms": lcp_ms, "cls": 0.42, "inp_ms": 480,
                    "fcp_ms": 3100, "ttfb_ms": 1800, "tbt_ms": 620},
            "lighthouse": {"performance": 54, "accessibility": 88,
                           "best_practices": 79, "seo": 90},
            "network": {"total_transfer_kb": 4820, "request_count": 118,
                        "render_blocking_css": 6},
            "main_thread": {"script_ms": 1820, "layout_ms": 240, "style_ms": 90,
                            "task_ms": 3100, "js_heap_kb": 24500, "dom_nodes": 3200},
        },
        "resource_timings": [
            {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140, "duration_ms": 390},
            {"name": "/app.js", "type": "script", "transfer_kb": 480, "duration_ms": 120},
        ],
        "captures": captures or {},
    })


@pytest.fixture
def conn():
    connection = sql.connect(":memory:")
    yield connection
    connection.close()


# --------------------------------------------------------------------------- #
# sql: schema + round-trip
# --------------------------------------------------------------------------- #
def test_connect_creates_schema_and_version(conn):
    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"runs", "resource_timings", "schema_meta"} <= tables
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert version == str(sql.SCHEMA_VERSION)


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "nested" / "runs.sqlite"
    first = sql.connect(path)
    sql.insert_run(first, make_run())
    first.close()

    second = sql.connect(path)  # re-opening must not wipe or fail
    assert sql.count_runs(second) == 1
    second.close()


def test_insert_and_get_run_round_trips_exactly(conn):
    original = make_run()
    sql.insert_run(conn, original)
    loaded = sql.get_run(conn, original.run_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == original.model_dump(mode="json")


def test_get_missing_run_returns_none(conn):
    assert sql.get_run(conn, "nope") is None


def test_flat_metric_columns_are_queryable(conn):
    """Metrics are columns, not an opaque blob — the report layer queries them."""
    sql.insert_run(conn, make_run(lcp_ms=6200))
    row = conn.execute(
        "SELECT lcp_ms, cls, tbt_ms, lh_performance, dom_nodes, page_name FROM runs"
    ).fetchone()
    assert row["lcp_ms"] == 6200
    assert row["cls"] == 0.42
    assert row["tbt_ms"] == 620
    assert row["lh_performance"] == 54
    assert row["dom_nodes"] == 3200
    assert row["page_name"] == "homepage"


def test_resource_timings_stored_in_child_table(conn):
    run = make_run()
    sql.insert_run(conn, run)
    timings = sql.get_resource_timings(conn, run.run_id)
    assert [t["name"] for t in timings] == ["/hero.mp4", "/app.js"]  # largest first
    assert timings[0]["transfer_kb"] == 2140


def test_duplicate_run_id_rejected_by_default(conn):
    run = make_run()
    sql.insert_run(conn, run)
    with pytest.raises(sql.StoreError, match="already stored"):
        sql.insert_run(conn, run)


def test_replace_overwrites_without_duplicating_timings(conn):
    run = make_run()
    sql.insert_run(conn, run)
    sql.insert_run(conn, make_run(lcp_ms=2200), replace=True)

    assert sql.count_runs(conn) == 1
    assert sql.get_run(conn, run.run_id).metrics.cwp.lcp_ms == 2200
    assert len(sql.get_resource_timings(conn, run.run_id)) == 2  # not 4


def test_insert_runs_persists_a_whole_campaign(conn):
    runs = [
        make_run("run_a", page="homepage"),
        make_run("run_b", page="pdp"),
        make_run("run_c", page="plp"),
    ]
    assert sql.insert_runs(conn, runs) == ["run_a", "run_b", "run_c"]
    assert sql.count_runs(conn) == 3


# --------------------------------------------------------------------------- #
# sql: querying
# --------------------------------------------------------------------------- #
@pytest.fixture
def populated(conn):
    sql.insert_runs(conn, [
        make_run("run_a", page="homepage", device="mid-mobile",
                 created_at="2026-01-08T10:00:00Z"),
        make_run("run_b", page="homepage", device="desktop",
                 created_at="2026-01-08T11:00:00Z"),
        make_run("run_c", page="pdp", device="mid-mobile",
                 created_at="2026-01-08T12:00:00Z"),
    ])
    return conn


def test_list_runs_returns_newest_first(populated):
    assert [r.run_id for r in sql.list_runs(populated)] == ["run_c", "run_b", "run_a"]


def test_list_runs_filters(populated):
    assert {r.run_id for r in sql.list_runs(populated, page="homepage")} == {"run_a", "run_b"}
    assert {r.run_id for r in sql.list_runs(populated, device="desktop")} == {"run_b"}
    assert sql.list_runs(populated, project="nonexistent") == []


def test_list_runs_combined_filters_and_limit(populated):
    runs = sql.list_runs(populated, page="homepage", device="mid-mobile")
    assert [r.run_id for r in runs] == ["run_a"]
    assert len(sql.list_runs(populated, limit=2)) == 2


def test_metric_history_is_chronological(populated):
    history = sql.metric_history(populated, "lcp_ms", project="storefront")
    assert [h["run_id"] for h in history] == ["run_a", "run_b", "run_c"]
    assert all(h["value"] == 6200 for h in history)
    assert history[0]["device"] == "mid-mobile"


def test_metric_history_rejects_unknown_metric(populated):
    """Column names are validated, never interpolated blindly."""
    with pytest.raises(sql.StoreError, match="Unknown metric"):
        sql.metric_history(populated, "lcp_ms; DROP TABLE runs")
    assert sql.count_runs(populated) == 3


def test_delete_run_removes_row_and_timings(populated):
    assert sql.delete_run(populated, "run_a") is True
    assert sql.get_run(populated, "run_a") is None
    assert sql.get_resource_timings(populated, "run_a") == []
    assert sql.delete_run(populated, "run_a") is False


def test_manual_run_without_metrics_persists(conn):
    """A text-only manual run has almost every metric NULL — still storable."""
    run = Run.model_validate({
        "run_id": "run_manual", "project": {"name": "p", "url": "https://e.com"},
        "page": {"name": "manual", "url": "https://e.com/"},
        "condition": {"device": "mid-mobile", "network": "slow-4g"},
        "meta": {"created_at": "2026-01-08T14:30:00Z", "source": "manual"},
        "problem": {"description": "feels slow"},
    })
    sql.insert_run(conn, run)
    loaded = sql.get_run(conn, "run_manual")
    assert loaded.metrics.cwp.lcp_ms is None
    assert loaded.problem.description == "feels slow"


# --------------------------------------------------------------------------- #
# artifacts: HAR scrubbing (SECURITY_PLAN.md §2.6)
# --------------------------------------------------------------------------- #
def har_with_secrets() -> dict:
    return {"log": {"version": "1.2", "entries": [{
        "request": {
            "url": "https://example.com/api?access_token=SECRET123&page=2",
            "headers": [
                {"name": "Cookie", "value": "session=abc123; uid=42"},
                {"name": "Authorization", "value": "Bearer eyJhbGciOi"},
                {"name": "Accept", "value": "text/html"},
            ],
            "cookies": [{"name": "session", "value": "abc123"}],
            "queryString": [
                {"name": "access_token", "value": "SECRET123"},
                {"name": "page", "value": "2"},
            ],
        },
        "response": {
            "headers": [
                {"name": "Set-Cookie", "value": "session=xyz789; HttpOnly"},
                {"name": "Content-Type", "value": "text/html"},
            ],
            "cookies": [{"name": "session", "value": "xyz789"}],
            "content": {"size": 1024},
        },
    }]}}


def test_scrub_har_redacts_credential_headers():
    scrubbed = art.scrub_har(har_with_secrets())
    request = scrubbed["log"]["entries"][0]["request"]
    headers = {h["name"]: h["value"] for h in request["headers"]}
    assert headers["Cookie"] == art.REDACTED
    assert headers["Authorization"] == art.REDACTED
    assert headers["Accept"] == "text/html"  # innocuous headers preserved


def test_scrub_har_redacts_configured_custom_headers():
    """A bot-allowlist token is a secret; it must not survive into the store.

    Its header name is project-specific, so it cannot live in the built-in
    SENSITIVE_HEADERS set — the configured names are passed in.
    """
    har = har_with_secrets()
    har["log"]["entries"][0]["request"]["headers"].append(
        {"name": "X-Akamai-Bot", "value": "super-secret-token"}
    )
    scrubbed = art.scrub_har(har, extra_headers=["X-Akamai-Bot"])
    headers = {h["name"]: h["value"] for h in scrubbed["log"]["entries"][0]["request"]["headers"]}
    assert headers["X-Akamai-Bot"] == art.REDACTED
    assert "super-secret-token" not in json.dumps(scrubbed)


def test_extra_header_matching_is_case_insensitive():
    har = har_with_secrets()
    har["log"]["entries"][0]["request"]["headers"].append(
        {"name": "x-akamai-bot", "value": "super-secret-token"}
    )
    scrubbed = art.scrub_har(har, extra_headers=["X-Akamai-Bot"])
    assert "super-secret-token" not in json.dumps(scrubbed)


def test_scrub_har_without_extra_headers_is_unchanged_behaviour():
    """Omitting extra_headers must behave exactly as before the parameter existed."""
    har = har_with_secrets()
    assert art.scrub_har(har) == art.scrub_har(har, extra_headers=None)


def test_scrub_har_file_honours_extra_headers(tmp_path):
    har = har_with_secrets()
    har["log"]["entries"][0]["request"]["headers"].append(
        {"name": "X-Akamai-Bot", "value": "super-secret-token"}
    )
    source = tmp_path / "in.har"
    source.write_text(json.dumps(har), encoding="utf-8")
    target = art.scrub_har_file(source, tmp_path / "out.har", extra_headers=["X-Akamai-Bot"])
    assert "super-secret-token" not in target.read_text(encoding="utf-8")


def test_store_artifacts_redacts_extra_headers_on_the_way_in(tmp_path):
    """The store must never receive an unredacted allowlist token."""
    har = har_with_secrets()
    har["log"]["entries"][0]["request"]["headers"].append(
        {"name": "X-Akamai-Bot", "value": "super-secret-token"}
    )
    source = tmp_path / "capture.har"
    source.write_text(json.dumps(har), encoding="utf-8")
    run = make_run(captures={"har": str(source)})

    stored = art.store_artifacts(tmp_path / "store", run, extra_headers=["X-Akamai-Bot"])
    content = open(stored["har"], encoding="utf-8").read()
    assert "super-secret-token" not in content


def test_scrub_har_redacts_set_cookie_on_responses():
    scrubbed = art.scrub_har(har_with_secrets())
    response = scrubbed["log"]["entries"][0]["response"]
    headers = {h["name"]: h["value"] for h in response["headers"]}
    assert headers["Set-Cookie"] == art.REDACTED
    assert headers["Content-Type"] == "text/html"


def test_scrub_har_redacts_cookie_arrays_and_query_params():
    scrubbed = art.scrub_har(har_with_secrets())
    entry = scrubbed["log"]["entries"][0]
    assert entry["request"]["cookies"][0]["value"] == art.REDACTED
    assert entry["response"]["cookies"][0]["value"] == art.REDACTED
    query = {q["name"]: q["value"] for q in entry["request"]["queryString"]}
    assert query["access_token"] == art.REDACTED
    assert query["page"] == "2"


def test_scrub_har_redacts_token_in_request_url():
    scrubbed = art.scrub_har(har_with_secrets())
    url = scrubbed["log"]["entries"][0]["request"]["url"]
    assert "SECRET123" not in url
    assert "page=2" in url


def test_no_secret_survives_anywhere_in_serialized_har():
    """The strongest check: grep the whole output for the raw secrets."""
    serialized = json.dumps(art.scrub_har(har_with_secrets()))
    for secret in ("SECRET123", "abc123", "xyz789", "eyJhbGciOi"):
        assert secret not in serialized, f"{secret!r} leaked into stored HAR"


def test_redact_url_strips_userinfo():
    assert art.redact_url("https://user:pass@example.com/x") == "https://example.com/x"


def test_redact_url_leaves_clean_urls_untouched():
    assert art.redact_url("https://example.com/a?page=2") == "https://example.com/a?page=2"
    assert art.redact_url("") == ""


def test_scrub_har_tolerates_minimal_and_odd_shapes():
    assert art.scrub_har({"log": {}})["log"] == {}
    assert art.scrub_har({"log": {"entries": []}})["log"]["entries"] == []
    with pytest.raises(art.ArtifactError):
        art.scrub_har("not-a-har")


def test_scrub_har_does_not_mutate_the_input():
    original = har_with_secrets()
    art.scrub_har(original)
    headers = {h["name"]: h["value"] for h in original["log"]["entries"][0]["request"]["headers"]}
    assert headers["Cookie"] == "session=abc123; uid=42"


# --------------------------------------------------------------------------- #
# artifacts: persistence
# --------------------------------------------------------------------------- #
@pytest.fixture
def captured(tmp_path):
    """A run whose captures point at real files in a scratch dir."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "run_1.png").write_bytes(b"\x89PNG fake")
    (raw / "run_1.trace.zip").write_bytes(b"PK fake trace")
    (raw / "run_1.har").write_text(json.dumps(har_with_secrets()), encoding="utf-8")
    run = make_run(captures={
        "screenshot": str(raw / "run_1.png"),
        "har": str(raw / "run_1.har"),
        "trace": str(raw / "run_1.trace.zip"),
    })
    return run, raw


def test_store_artifacts_writes_run_scoped_layout(tmp_path, captured):
    run, _ = captured
    stored = art.store_artifacts(tmp_path / "store", run)
    expected = tmp_path / "store" / "storefront" / "homepage" / run.run_id
    assert stored["screenshot"] == str(expected / "screenshot.png")
    assert stored["har"] == str(expected / "capture.har")
    assert stored["trace"] == str(expected / "trace.zip")
    assert (expected / "screenshot.png").read_bytes() == b"\x89PNG fake"


def test_stored_har_is_scrubbed_on_the_way_in(tmp_path, captured):
    """The store must never hold an unredacted HAR, even briefly."""
    run, _ = captured
    stored = art.store_artifacts(tmp_path / "store", run)
    content = open(stored["har"], encoding="utf-8").read()
    for secret in ("SECRET123", "abc123", "xyz789", "eyJhbGciOi"):
        assert secret not in content


def test_store_artifacts_copies_by_default(tmp_path, captured):
    run, raw = captured
    art.store_artifacts(tmp_path / "store", run)
    assert (raw / "run_1.png").exists()  # source preserved


def test_store_artifacts_can_move(tmp_path, captured):
    run, raw = captured
    art.store_artifacts(tmp_path / "store", run, move=True)
    assert not (raw / "run_1.png").exists()
    assert not (raw / "run_1.har").exists()


def test_missing_captures_reported_as_none(tmp_path):
    run = make_run(captures={})
    assert art.store_artifacts(tmp_path / "store", run) == {
        "screenshot": None, "har": None, "trace": None
    }


def test_captures_pointing_at_absent_files_are_none(tmp_path):
    run = make_run(captures={"screenshot": str(tmp_path / "gone.png")})
    assert art.store_artifacts(tmp_path / "store", run)["screenshot"] is None


def test_load_artifact_paths_finds_stored_files(tmp_path, captured):
    run, _ = captured
    art.store_artifacts(tmp_path / "store", run)
    found = art.load_artifact_paths(tmp_path / "store", run)
    assert found["screenshot"] and found["har"] and found["trace"]


def test_load_artifact_paths_none_when_nothing_stored(tmp_path):
    found = art.load_artifact_paths(tmp_path / "store", make_run())
    assert set(found.values()) == {None}


def test_path_segments_cannot_escape_the_store_root(tmp_path):
    """A crafted project/page name must not traverse out of the root."""
    run = make_run("../../etc/run", page="../../..")
    directory = art.run_dir(tmp_path / "store", "../../evil", run.page.name, run.run_id)
    assert (tmp_path / "store") in directory.parents
    assert ".." not in directory.parts


def test_scrub_har_file_reports_bad_input(tmp_path):
    bad = tmp_path / "bad.har"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(art.ArtifactError, match="Malformed HAR"):
        art.scrub_har_file(bad, tmp_path / "out.har")
    with pytest.raises(art.ArtifactError, match="not found"):
        art.scrub_har_file(tmp_path / "missing.har", tmp_path / "out.har")
