"""Env resolution, request shape, and retry policy. No network.

``resolve_grafana_env`` runs the base URL through the real
``normalize.url_safety.validate_url`` (SECURITY_PLAN 2.2), which resolves the
hostname by design — that guard is not weakened for this feature. To keep
these tests network-free, ``_lookup`` is monkeypatched to a public IP, the
same pattern ``tests/unit/browser_test.py`` uses for the identical guard.
"""
import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from ingest.grafana.client import (
    GrafanaClient,
    GrafanaEnv,
    GrafanaError,
    MissingGrafanaConfigError,
    resolve_grafana_env,
)
from normalize import url_safety


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """Force DNS resolution to a public IP so validate_url(resolve=True) passes."""
    monkeypatch.setattr(url_safety, "_lookup", lambda host: {"8.8.8.8"})


FULL_ENV = {
    "GRAFANA_BASE_URL": "https://grafana.example.com",
    "GRAFANA_TOKEN": "glsa_secret_value",
    "GRAFANA_DATASOURCE_UID": "abc123",
    "GRAFANA_TABLE": "tenant.mpulse",
}


def test_resolve_reads_all_four_variables():
    env = resolve_grafana_env(FULL_ENV)
    assert env.base_url == "https://grafana.example.com"
    assert env.datasource_uid == "abc123"
    assert env.table == "tenant.mpulse"


@pytest.mark.parametrize("missing", sorted(FULL_ENV))
def test_each_missing_variable_is_named(missing):
    partial = {k: v for k, v in FULL_ENV.items() if k != missing}
    with pytest.raises(MissingGrafanaConfigError) as exc:
        resolve_grafana_env(partial)
    assert missing in str(exc.value)


def test_token_never_appears_in_an_error_message():
    """SECURITY_PLAN 2.8: the value is never echoed, only the variable name."""
    broken = dict(FULL_ENV, GRAFANA_BASE_URL="")
    with pytest.raises(MissingGrafanaConfigError) as exc:
        resolve_grafana_env(broken)
    assert "glsa_secret_value" not in str(exc.value)


def test_trailing_slash_on_base_url_is_normalised():
    env = resolve_grafana_env(dict(FULL_ENV, GRAFANA_BASE_URL="https://g.example.com/"))
    assert env.base_url == "https://g.example.com"


def test_non_https_base_url_is_rejected():
    with pytest.raises(GrafanaError):
        resolve_grafana_env(dict(FULL_ENV, GRAFANA_BASE_URL="http://g.example.com"))


class _FakeOpener:
    """Records requests and replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return BytesIO(json.dumps(outcome).encode("utf-8"))


def _client(responses):
    return GrafanaClient(
        resolve_grafana_env(FULL_ENV), timeout_s=5, opener=_FakeOpener(responses)
    )


def test_query_posts_bearer_token_and_correct_path():
    client = _client([{"results": {}}])
    client.query({"vitals": "SELECT 1"}, window="7d")
    request = client._opener.requests[0]
    assert request.full_url == "https://grafana.example.com/api/ds/query"
    assert request.get_header("Authorization") == "Bearer glsa_secret_value"
    assert request.get_method() == "POST"


def test_query_body_carries_one_entry_per_ref_id():
    client = _client([{"results": {}}])
    client.query({"vitals": "SELECT 1", "assets": "SELECT 2"}, window="24h")
    body = json.loads(client._opener.requests[0].data.decode("utf-8"))
    assert body["from"] == "now-24h" and body["to"] == "now"
    assert {q["refId"] for q in body["queries"]} == {"vitals", "assets"}
    assert body["queries"][0]["datasource"]["uid"] == "abc123"


def test_query_body_carries_interval_max_points_and_format_per_query_type():
    """Whole-branch review item 4: intervalMs/maxDataPoints were omitted and
    format was sent as 1 (table) even for the three timeseries refIds. This
    cannot be validated against a real Grafana offline - only that the
    client sends what the design's §3.1 example shows.
    """
    client = _client([{"results": {}}])
    client.query(
        {"vitals": "SELECT 1", "sessions": "SELECT 2", "assets": "SELECT 3",
         "headline": "SELECT 4"},
        window="7d",
    )
    body = json.loads(client._opener.requests[0].data.decode("utf-8"))
    by_ref = {q["refId"]: q for q in body["queries"]}

    for ref_id, query in by_ref.items():
        assert isinstance(query["intervalMs"], int) and query["intervalMs"] > 0
        assert query["maxDataPoints"] > 0

    # Timeseries panels: format 0. Table panels: format 1 (design §3.1/§3.2).
    # `headline` is ungrouped (one row, no time column), so it is a table
    # query like the five others, not a timeseries one.
    assert by_ref["vitals"]["format"] == 0
    assert by_ref["sessions"]["format"] == 0
    assert by_ref["assets"]["format"] == 1
    assert by_ref["headline"]["format"] == 1


def test_interval_ms_is_not_absurdly_fine_over_a_long_window():
    """A 7-day window must not bucket down to (near) individual rows."""
    client = _client([{"results": {}}])
    client.query({"vitals": "SELECT 1"}, window="7d")
    body = json.loads(client._opener.requests[0].data.decode("utf-8"))
    interval_ms = body["queries"][0]["intervalMs"]
    # 7 days / 200 target buckets is on the order of tens of minutes; a
    # generous upper bound rules out per-second-scale bucketing.
    assert interval_ms >= 60_000


def test_per_query_error_raises_naming_the_ref_id():
    """Whole-branch review item 3: a rejected query used to parse to an
    all-None row and store a silent 'live' snapshot of em dashes. Design §8
    promises `ingest field` exits non-zero on a rejected query.
    """
    client = _client([{"results": {
        "vitals": {"frames": [{
            "schema": {"fields": [{"name": "time", "type": "time"}]},
            "data": {"values": [[]]},
        }]},
        "assets": {"error": "table X does not exist", "status": 500},
    }}])
    with pytest.raises(GrafanaError) as exc:
        client.query({"vitals": "SELECT 1", "assets": "SELECT 2"}, window="7d")
    message = str(exc.value)
    assert "assets" in message
    assert "table X does not exist" in message


def test_a_clean_mixed_response_with_no_errors_does_not_raise():
    client = _client([{"results": {
        "vitals": {"frames": []},
        "assets": {"frames": []},
    }}])
    result = client.query({"vitals": "SELECT 1", "assets": "SELECT 2"}, window="7d")
    assert set(result) == {"vitals", "assets"}


def test_server_error_is_retried_once_then_succeeds():
    boom = HTTPError("u", 503, "unavailable", {}, None)
    client = _client([boom, {"results": {"ok": {}}}])
    assert client.query({"vitals": "SELECT 1"}, window="7d") == {"ok": {}}
    assert len(client._opener.requests) == 2


def test_client_error_is_not_retried():
    boom = HTTPError("u", 401, "unauthorized", {}, None)
    client = _client([boom])
    with pytest.raises(GrafanaError) as exc:
        client.query({"vitals": "SELECT 1"}, window="7d")
    assert "401" in str(exc.value)
    assert len(client._opener.requests) == 1


def test_connection_failure_is_retried_once_then_raises():
    client = _client([URLError("down"), URLError("down")])
    with pytest.raises(GrafanaError):
        client.query({"vitals": "SELECT 1"}, window="7d")
    assert len(client._opener.requests) == 2


def test_malformed_json_response_raises_grafana_error():
    class _BadOpener:
        requests = []

        def open(self, request, timeout=None):
            self.requests.append(request)
            return BytesIO(b"<html>gateway</html>")

    client = GrafanaClient(
        resolve_grafana_env(FULL_ENV), timeout_s=5, opener=_BadOpener()
    )
    with pytest.raises(GrafanaError):
        client.query({"vitals": "SELECT 1"}, window="7d")


def test_client_exposes_its_table_publicly():
    client = _client([{"results": {}}])
    assert client.table == "tenant.mpulse"


def test_token_is_not_reachable_through_repr():
    """SECURITY_PLAN 2.8: an unhandled traceback must not print the credential."""
    env = resolve_grafana_env(FULL_ENV)
    assert "glsa_secret_value" not in repr(env)
    assert "glsa_secret_value" not in str(env)
    assert env.token == "glsa_secret_value"  # still readable by the client
