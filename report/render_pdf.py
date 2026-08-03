"""Chromium print-to-PDF (PROJECT_SPEC §10 Phase 5).

The engine is the same browser the ingestion layer already drives, which is
why there is no second PDF library in this project: the HTML is authored for
print, and Chromium's print pipeline is what the CSS was written against.

``page_factory`` is the injection seam, matching ``ingest/browser/runner.py``:
tests hand in a fake page, so a mistake in the print options is caught by the
offline suite while real generation stays in the e2e run.

The HTML is delivered with ``set_content`` rather than by navigating to a
``file://`` URL. There is then no navigation and no origin — nothing for
``normalize.url_safety`` to be responsible for, and no path handling to get
wrong on Windows.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Dict

# Margins are zero because the page geometry lives in the stylesheet's @page
# rule. Splitting it across two places is how headers end up overlapping text.
PDF_OPTIONS: Dict[str, Any] = {
    "format": "A4",
    "print_background": True,
    "prefer_css_page_size": True,
    "margin": {"top": "0", "right": "0", "bottom": "0", "left": "0"},
}


def render_pdf(html: str, *, page_factory: Callable[[], Any]) -> bytes:
    """Render an HTML document to PDF bytes.

    Raises ``RuntimeError`` when the browser returns something that is not a
    usable PDF — an empty buffer or an error page — rather than writing a file
    that only fails when somebody opens it.
    """
    with page_factory() as page:
        page.set_content(html, wait_until="load")
        pdf = page.pdf(**PDF_OPTIONS)

    if not pdf:
        raise RuntimeError("Chromium returned an empty PDF buffer.")
    if not pdf.startswith(b"%PDF"):
        raise RuntimeError(
            "Chromium returned output that is not a PDF; the print pipeline "
            "did not run."
        )
    return pdf


@contextmanager
def chromium_page_factory():
    """A real headless Chromium page. Imported lazily so tests need no browser."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            yield page
        finally:
            browser.close()
