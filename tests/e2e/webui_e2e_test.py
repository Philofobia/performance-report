"""End-to-end: a real browser fills the real form.

Every other test drives the WSGI callable directly, which proves the handler
is correct and proves nothing about the markup. An unclosed tag, a missing
`name`, a button outside the form — all of them pass the integration suite and
none of them submit. This is the only test that would catch them.
"""
from __future__ import annotations

import json
import threading
from wsgiref.simple_server import make_server

import pytest

from webui.app import Application

pytestmark = pytest.mark.e2e


@pytest.fixture()
def server(tmp_path):
    """The app on an ephemeral loopback port, torn down with the test."""
    app = Application(
        output_dir=tmp_path / "processed",
        devices=["mid-mobile", "desktop"],
        networks=["slow-4g", "fast-3g"],
    )
    httpd = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/", tmp_path / "processed"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_browser_can_fill_and_submit_the_form(server):
    from playwright.sync_api import sync_playwright

    url, output_dir = server

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)

        page.fill("#f-page_url", "https://example.com/")
        page.fill("#f-page", "homepage")
        page.fill("#f-problem", "Hero video delays LCP on 3G")
        page.fill("#f-lcp_ms", "6200")
        page.fill("#f-cls", "0.42")
        page.select_option("#f-device", "mid-mobile")

        page.click("button[type=submit]")
        page.wait_for_selector(".banner--ok")

        banner = page.text_content(".banner--ok")
        browser.close()

    written = list(output_dir.glob("*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["run_id"] in banner
    assert payload["metrics"]["cwp"]["lcp_ms"] == 6200
    assert payload["page"]["name"] == "homepage"


def test_the_browser_blocks_an_out_of_range_value_before_it_is_sent(server):
    """`max` came from the schema; this proves the browser honours it."""
    from playwright.sync_api import sync_playwright

    url, output_dir = server

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)
        page.fill("#f-page_url", "https://example.com/")
        page.fill("#f-cls", "1.5")
        page.click("button[type=submit]")
        valid = page.eval_on_selector("#f-cls", "el => el.checkValidity()")
        browser.close()

    assert valid is False
    assert not list(output_dir.glob("*.json"))
