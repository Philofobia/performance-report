"""Unit tests for report/render_pdf.py.

No browser is launched here: the Playwright page is a fake, so a typo in the
`page.pdf()` call is caught by the offline suite instead of only by the e2e
run. Actual PDF generation is covered in tests/e2e/report_pdf_e2e_test.py.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from report.render_pdf import PDF_OPTIONS, render_pdf


class FakePage:
    def __init__(self, pdf_bytes=b"%PDF-1.4 fake"):
        self.content = None
        self.pdf_kwargs = None
        self.set_content_kwargs = None
        self._pdf_bytes = pdf_bytes

    def set_content(self, html, **kwargs):
        self.content = html
        self.set_content_kwargs = kwargs

    def pdf(self, **kwargs):
        self.pdf_kwargs = kwargs
        return self._pdf_bytes


@contextmanager
def factory_for(page):
    yield page


def test_html_reaches_the_page_and_pdf_bytes_come_back():
    page = FakePage()
    result = render_pdf("<h1>hello</h1>", page_factory=lambda: factory_for(page))
    assert page.content == "<h1>hello</h1>"
    assert result.startswith(b"%PDF")


def test_content_is_set_rather_than_navigated_to():
    # set_content means no navigation, no origin, nothing for the SSRF gate
    # to be responsible for. A file:// navigation would reintroduce both.
    page = FakePage()
    render_pdf("<h1>x</h1>", page_factory=lambda: factory_for(page))
    assert not hasattr(page, "goto_url")
    assert page.set_content_kwargs == {"wait_until": "load"}


def test_print_options_are_a4_with_backgrounds():
    page = FakePage()
    render_pdf("<h1>x</h1>", page_factory=lambda: factory_for(page))
    assert page.pdf_kwargs["format"] == "A4"
    assert page.pdf_kwargs["print_background"] is True
    assert page.pdf_kwargs == PDF_OPTIONS


def test_an_empty_pdf_is_reported_rather_than_returned():
    page = FakePage(pdf_bytes=b"")
    with pytest.raises(RuntimeError, match="empty"):
        render_pdf("<h1>x</h1>", page_factory=lambda: factory_for(page))


def test_output_that_is_not_a_pdf_is_reported():
    page = FakePage(pdf_bytes=b"<html>oops</html>")
    with pytest.raises(RuntimeError, match="not a PDF"):
        render_pdf("<h1>x</h1>", page_factory=lambda: factory_for(page))
