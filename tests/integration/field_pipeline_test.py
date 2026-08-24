"""ingest field: hosts from targets.yaml, fetch, persist. No network."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from config.load import Settings
from ingest.field import fetch_snapshot, hosts_for, main
from store import sql

FIXTURE = Path(__file__).parent.parent / "fixtures" / "grafana_response.json"


class _StubClient:
    """Replays the recorded response for whatever SQL it is handed."""

    #: A public property in the real client (Task 4); a class attribute here
    #: plays the same role without needing GrafanaEnv at all.
    table = "tenant.mpulse"

    def __init__(self):
        self.calls = []

    def query(self, sql_by_ref, *, window):
        self.calls.append((sorted(sql_by_ref), window))
        return json.loads(FIXTURE.read_text(encoding="utf-8"))["results"]


class _Targets:
    def __init__(self, pages):
        self.pages = pages


class _Page:
    def __init__(self, url):
        self.url = url


def test_hosts_are_derived_from_target_urls_deduped_and_sorted():
    targets = _Targets([
        _Page("https://www.oakley.com/en-us"),
        _Page("https://www.oakley.com/en-us/category/sunglasses"),
        _Page("https://m.oakley.com/x"),
    ])
    assert hosts_for(targets) == ["m.oakley.com", "www.oakley.com"]


def test_hosts_ignores_a_page_with_no_hostname():
    assert hosts_for(_Targets([_Page("not-a-url"), _Page("https://a.com/x")])) == ["a.com"]


def test_fetch_issues_one_call_carrying_all_eight_queries():
    client = _StubClient()
    snapshot = fetch_snapshot(
        Settings(), project="oakley", hosts=["www.oakley.com"], client=client,
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    refs, window = client.calls[0]
    assert len(client.calls) == 1
    assert len(refs) == 8
    assert window == "7d"
    assert snapshot.project == "oakley"


def test_window_is_resolved_to_absolute_utc():
    """'now-7d' is not reproducible; the stored snapshot must name its week."""
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    snapshot = fetch_snapshot(
        Settings(), project="oakley", hosts=["www.oakley.com"],
        client=_StubClient(), now=now,
    )
    assert snapshot.window_to == now
    assert (snapshot.window_to - snapshot.window_from).days == 7


def test_fetch_raises_when_no_table_can_be_resolved():
    """No ``table`` kwarg and a client with none either must fail loudly,
    never render the literal string ``{table}`` into SQL that only breaks
    once it reaches real ClickHouse."""

    class _NoTable:
        def query(self, sql_by_ref, *, window):
            raise AssertionError("must not reach the network")

    with pytest.raises(ValueError, match="GRAFANA_TABLE"):
        fetch_snapshot(Settings(), project="oakley", hosts=["a.com"],
                       client=_NoTable())


def test_main_persists_and_reports(tmp_path, monkeypatch, capsys):
    store_path = tmp_path / "runs.sqlite"
    monkeypatch.setattr("ingest.field._build_client", lambda settings: _StubClient())
    monkeypatch.setattr(
        "ingest.field.load_settings",
        lambda *a, **k: Settings(storage={"sqlite_path": str(store_path)}),
    )
    assert main(["--project", "oakley", "--hosts", "www.oakley.com"]) == 0

    conn = sql.connect(store_path)
    sql.init_schema(conn)
    try:
        assert sql.get_latest_snapshot(conn, "oakley") is not None
    finally:
        conn.close()
    assert "www.oakley.com" in capsys.readouterr().out


def test_main_exits_one_on_duplicate_snapshot_without_replace(tmp_path, monkeypatch, capsys):
    """insert_snapshot's StoreError on a duplicate id must surface as this
    stage's own clean message and exit 1 - never as a raw traceback, and
    never as Python's crash-implied exit 1 rather than this stage's
    deliberate one."""

    class _FrozenDatetime(datetime):
        """Pins ``datetime.now()`` so two ``main()`` calls mint the same
        ``snapshot_id`` (it is derived from ``fetched_at`` to the second)."""

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 24, tzinfo=timezone.utc)

    monkeypatch.setattr("ingest.field.datetime", _FrozenDatetime)
    monkeypatch.setattr("ingest.field._build_client", lambda settings: _StubClient())
    monkeypatch.setattr(
        "ingest.field.load_settings",
        lambda *a, **k: Settings(storage={"sqlite_path": str(tmp_path / "runs.sqlite")}),
    )

    argv = ["--project", "oakley", "--hosts", "www.oakley.com"]
    assert main(argv) == 0
    assert main(argv) == 1  # same snapshot_id, no --replace
    err = capsys.readouterr().err
    assert "--replace" in err


def test_main_exits_non_zero_when_grafana_is_unreachable(tmp_path, monkeypatch):
    from ingest.grafana.client import GrafanaError

    def _boom(settings):
        raise GrafanaError("could not reach Grafana")

    monkeypatch.setattr("ingest.field._build_client", _boom)
    monkeypatch.setattr(
        "ingest.field.load_settings",
        lambda *a, **k: Settings(storage={"sqlite_path": str(tmp_path / "r.sqlite")}),
    )
    assert main(["--project", "oakley", "--hosts", "www.oakley.com"]) == 1
