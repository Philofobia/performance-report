"""Integration tests for the manual-entry WSGI application.

The app is a plain callable over two dicts, so these tests build an `environ`
and call it — no socket, no port, no browser. That is the whole reason the
WSGI shape was chosen over BaseHTTPRequestHandler; see the design doc §3.1.
"""
from __future__ import annotations

import io
import json
from urllib.parse import urlencode

import pytest

from webui import form
from webui.app import FORM_CONTENT_TYPE, MAX_BODY_BYTES, Application


@pytest.fixture()
def app(tmp_path):
    return Application(
        output_dir=tmp_path / "processed",
        devices=["mid-mobile", "desktop"],
        networks=["slow-4g", "fast-3g"],
    )


def call(app, method="GET", path="/", query="", body="",
         content_type=FORM_CONTENT_TYPE, content_length=None):
    """Drive the WSGI callable directly. Returns (status, headers, body)."""
    raw = body.encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(raw)) if content_length is None else content_length,
        "wsgi.input": io.BytesIO(raw),
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = app(environ, start_response)
    return captured["status"], captured["headers"], b"".join(chunks).decode("utf-8")


def submission(**overrides):
    values = {
        "project": "storefront",
        "page": "homepage",
        "page_url": "https://example.com/",
        "device": "mid-mobile",
        "network": "slow-4g",
        "problem": "Homepage LCP spikes to 6s on 3G",
        "lcp_ms": "6200",
        "cls": "0.42",
    }
    values.update(overrides)
    return urlencode(values)


# --- GET / ------------------------------------------------------------------


def test_get_renders_an_input_for_every_declared_field(app):
    status, headers, body = call(app)
    assert status.startswith("200")
    assert headers["Content-Type"].startswith("text/html")
    for field in form.FIELDS:
        assert f'name="{field.name}"' in body


def test_range_hints_come_from_the_schema(app):
    """Range hints are the schema's numbers, not the template's.

    CLS carries `min="0"` and *no* max, because the schema bounds it below
    only; Lighthouse carries the schema's 0..100. A template with its own
    numbers would still have claimed `max="1"` for CLS after the schema
    stopped saying so.
    """
    _, _, body = call(app)
    assert 'name="cls"' in body
    assert 'max="100"' in body          # Lighthouse scores


def test_device_and_network_render_the_configured_presets(app):
    _, _, body = call(app)
    assert '<option value="mid-mobile"' in body
    assert '<option value="fast-3g"' in body


def test_the_selects_preselect_the_run_defaults(tmp_path):
    """Otherwise the first option wins, and it is not the default condition.

    `config/settings.yaml` defaults a run to slow-4g; `networks.yaml` lists
    `online` first. A form that submitted `online` for an untouched select
    would file runs under a condition the operator never chose.
    """
    app = Application(
        output_dir=tmp_path / "processed",
        devices=["mid-mobile", "desktop"],
        networks=["online", "fast-3g", "slow-4g"],
        defaults={"device": "desktop", "network": "slow-4g"},
    )
    _, _, body = call(app)
    assert '<option value="desktop" selected' in body
    assert '<option value="slow-4g" selected' in body
    assert '<option value="online" selected' not in body


def test_a_submitted_value_beats_the_default(tmp_path):
    app = Application(
        output_dir=tmp_path / "processed",
        devices=["mid-mobile", "desktop"],
        networks=["online", "fast-3g", "slow-4g"],
        defaults={"device": "desktop", "network": "slow-4g"},
    )
    _, _, body = call(app, query=urlencode({"device": "mid-mobile"}))
    assert '<option value="mid-mobile" selected' in body
    assert '<option value="desktop" selected' not in body


def test_the_saved_banner_names_the_run_and_prefills_the_context(app):
    _, _, body = call(app, query=urlencode({
        "saved": "run_20260818_101500_ab12",
        "page": "homepage",
        "page_url": "https://example.com/",
    }))
    assert "run_20260818_101500_ab12" in body
    assert 'value="homepage"' in body


def test_the_stylesheet_is_served_as_css(app):
    status, headers, body = call(app, path="/static/style.css")
    assert status.startswith("200")
    assert headers["Content-Type"].startswith("text/css")
    assert "--paper" in body


# --- routing ----------------------------------------------------------------


def test_an_unknown_path_is_404(app):
    status, _, _ = call(app, path="/nope")
    assert status.startswith("404")


def test_posting_to_the_form_path_is_405(app):
    status, headers, _ = call(app, method="POST", path="/", body=submission())
    assert status.startswith("405")
    assert headers.get("Allow") == "GET"


def test_getting_the_runs_path_is_405(app):
    status, headers, _ = call(app, path="/runs")
    assert status.startswith("405")
    assert headers.get("Allow") == "POST"


# --- POST /runs, happy path -------------------------------------------------


def test_a_valid_submission_writes_one_run_json(app, tmp_path):
    status, headers, _ = call(app, method="POST", path="/runs", body=submission())
    assert status.startswith("303")

    written = list((tmp_path / "processed").glob("*.json"))
    assert len(written) == 1

    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["metrics"]["cwp"]["lcp_ms"] == 6200
    assert payload["meta"]["source"] == "manual"
    assert payload["meta"]["runner"] == form.RUNNER


def test_the_redirect_names_the_run_and_carries_the_context_forward(app):
    _, headers, _ = call(app, method="POST", path="/runs", body=submission())
    location = headers["Location"]
    assert location.startswith("/?")
    assert "saved=run_" in location
    assert "page=homepage" in location
    assert "lcp_ms" not in location          # metrics must not be sticky


def test_the_written_file_is_named_for_the_run_id(app, tmp_path):
    _, headers, _ = call(app, method="POST", path="/runs", body=submission())
    run_id = headers["Location"].split("saved=")[1].split("&")[0]
    assert (tmp_path / "processed" / f"{run_id}.json").exists()


def test_two_submissions_do_not_collide(app, tmp_path):
    call(app, method="POST", path="/runs", body=submission())
    call(app, method="POST", path="/runs", body=submission())
    assert len(list((tmp_path / "processed").glob("*.json"))) == 2


def test_the_ui_and_the_cli_produce_the_same_run(app, tmp_path):
    """The parity test.

    The UI is a front door to manual ingestion, not a second ingestion path.
    Identical inputs must produce identical payloads — everything but the
    fields that are *supposed* to differ: the id, the timestamp, and the
    runner that records which door was used.
    """
    from ingest.manual import main as manual_main

    cli_output = tmp_path / "cli.json"
    assert manual_main([
        "--project", "storefront", "--page", "homepage",
        "--page-url", "https://example.com/",
        "--device", "mid-mobile", "--network", "slow-4g",
        "--problem", "Homepage LCP spikes to 6s on 3G",
        "--lcp-ms", "6200", "--cls", "0.42",
        "--output", str(cli_output),
    ]) == 0

    call(app, method="POST", path="/runs", body=submission())
    ui_output = next((tmp_path / "processed").glob("*.json"))

    def stable(payload):
        """Drop only what is *meant* to differ between the two doors."""
        payload.pop("run_id")
        for key in ("created_at", "runner"):
            payload["meta"].pop(key)
        return payload

    assert stable(json.loads(ui_output.read_text(encoding="utf-8"))) == \
           stable(json.loads(cli_output.read_text(encoding="utf-8")))


def test_the_output_directory_is_created_on_demand(tmp_path):
    app = Application(output_dir=tmp_path / "deep" / "processed")
    status, _, _ = call(app, method="POST", path="/runs", body=submission())
    assert status.startswith("303")
    assert (tmp_path / "deep" / "processed").is_dir()


# --- POST /runs, refusals ---------------------------------------------------


def test_an_out_of_range_value_is_rejected_by_the_schema_not_the_ui(app, tmp_path):
    status, _, body = call(app, method="POST", path="/runs",
                           body=submission(cls="-0.1"))
    assert status.startswith("400")
    assert "cls" in body
    assert not list((tmp_path / "processed").glob("*.json"))


def test_a_rejected_submission_keeps_every_value_the_user_typed(app):
    _, _, body = call(app, method="POST", path="/runs",
                      body=submission(cls="-0.1", lcp_ms="6200"))
    assert 'value="6200"' in body
    assert 'value="-0.1"' in body
    assert "Homepage LCP spikes to 6s on 3G" in body


def test_a_missing_page_url_names_the_field_it_is_missing_from(app):
    status, _, body = call(app, method="POST", path="/runs",
                           body=submission(page_url=""))
    assert status.startswith("400")
    assert "page url" in body.lower()


def test_a_non_https_url_is_refused_by_the_ssrf_gate(app, tmp_path):
    status, _, body = call(app, method="POST", path="/runs",
                           body=submission(page_url="http://169.254.169.254/"))
    assert status.startswith("400")
    assert not list((tmp_path / "processed").glob("*.json"))


def test_a_non_numeric_metric_is_a_field_error_not_a_traceback(app):
    status, _, body = call(app, method="POST", path="/runs",
                           body=submission(lcp_ms="soon"))
    assert status.startswith("400")
    assert "must be a number" in body


def test_an_oversized_body_is_refused_unread(app, tmp_path):
    status, _, _ = call(app, method="POST", path="/runs",
                        body="problem=" + "x" * MAX_BODY_BYTES)
    assert status.startswith("413")
    assert not list((tmp_path / "processed").glob("*.json"))


def test_a_json_content_type_is_refused(app):
    status, _, _ = call(app, method="POST", path="/runs",
                        body=submission(), content_type="application/json")
    assert status.startswith("415")


def test_a_missing_content_length_is_refused(app):
    status, _, _ = call(app, method="POST", path="/runs", body=submission(),
                        content_length="")
    assert status.startswith("411")


def test_a_malformed_content_length_is_refused(app):
    status, _, _ = call(app, method="POST", path="/runs", body=submission(),
                        content_length="not-a-number")
    assert status.startswith("400")


def test_submitted_prose_is_escaped_on_the_way_back_out(app):
    """The error path echoes user text into HTML; it must not echo markup."""
    _, _, body = call(app, method="POST", path="/runs",
                      body=submission(cls="-0.1", problem="<script>alert(1)</script>"))
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_the_success_banner_escapes_what_it_reflects(app):
    _, _, body = call(app, query=urlencode({"saved": "<script>alert(1)</script>"}))
    assert "<script>alert(1)</script>" not in body
