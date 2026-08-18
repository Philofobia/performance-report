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
    """`max="1"` on CLS must be the schema's number, not the template's."""
    _, _, body = call(app)
    assert 'name="cls"' in body
    assert 'max="1"' in body
    assert 'max="100"' in body          # Lighthouse scores


def test_device_and_network_render_the_configured_presets(app):
    _, _, body = call(app)
    assert '<option value="mid-mobile"' in body
    assert '<option value="fast-3g"' in body


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
