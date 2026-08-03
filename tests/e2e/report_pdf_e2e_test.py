"""End-to-end PDF generation against real Chromium.

Skipped by the offline suite (`pytest -m "not e2e"`), which is the only place
in this repo where a browser is required for rendering.
"""
from __future__ import annotations

import pytest

from report.render_html import render_html
from report.render_pdf import chromium_page_factory, render_pdf
from tests.unit.render_html_test import a_report

pytestmark = pytest.mark.e2e


def test_real_chromium_produces_a_paginated_pdf():
    html = render_html(a_report(("homepage", "plp")))
    pdf = render_pdf(html, page_factory=chromium_page_factory)

    assert pdf.startswith(b"%PDF")
    # A blank page is roughly 1KB; a two-page report with charts is far larger.
    assert len(pdf) > 20_000
