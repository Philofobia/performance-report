"""End-to-end PDF generation against real Chromium.

Skipped by the offline suite (`pytest -m "not e2e"`), which is the only place
in this repo where a browser is required for rendering.
"""
from __future__ import annotations

import re

import numpy as np
import pytest
from PIL import Image

from analysis.reportmodel import Report
from report.images import embed_png, entry_key
from report.render_html import render_html
from report.render_pdf import chromium_page_factory, render_pdf
from tests.unit.render_html_test import a_report, an_appendix_entry

pytestmark = pytest.mark.e2e


def test_real_chromium_produces_a_paginated_pdf():
    html = render_html(a_report(("homepage", "plp")))
    pdf = render_pdf(html, page_factory=chromium_page_factory)

    assert pdf.startswith(b"%PDF")
    # A blank page is roughly 1KB; a two-page report with charts is far larger.
    assert len(pdf) > 20_000


def _a_realistic_screenshot(path):
    """A deterministic gradient PNG, not a flat colour swatch.

    A solid-colour image is worst-case input for any size-based PDF check:
    Chromium's Skia PDF writer FlateDecodes the raster stream, and a flat
    colour compresses to almost nothing — in a probe run against this exact
    pipeline, a report with a solid-colour screenshot embedded produced a
    *smaller* PDF than the same report without one (the empty-state caption
    text it replaces cost more bytes than the image added). A gradient gives
    the image real entropy, which is what an actual screenshot has, without
    sacrificing determinism (no RNG — every pixel is a pure function of its
    coordinates).
    """
    width, height = 1440, 900
    x = np.arange(width, dtype=np.uint8)
    y = np.arange(height, dtype=np.uint8)
    red = np.tile(x, (height, 1))
    green = np.tile(y.reshape(-1, 1), (1, width))
    blue = (red.astype(np.uint16) + green.astype(np.uint16)).astype(np.uint8)
    Image.fromarray(np.dstack([red, green, blue]), mode="RGB").save(path, format="PNG")
    return path


def _pdf_has_image_xobject(pdf: bytes, *, width: int, height: int) -> bool:
    """True when `pdf` contains an `/Image` XObject with these exact pixel
    dimensions.

    This is what actually distinguishes "the screenshot reached Chromium's
    print pipeline" from "the PDF changed size for some reason" — Chromium's
    PDF writer emits one `/XObject` dictionary with `/Subtype /Image` per
    raster image it embeds, carrying the image's `/Width` and `/Height` in
    pixels. Matching the exact dimensions `report/images.py` computed for
    this capture (not just *an* image existing somewhere) rules out a false
    positive from an unrelated image elsewhere in the document.
    """
    for marker in re.finditer(rb"/Subtype\s*/Image", pdf):
        window = pdf[marker.end():marker.end() + 200]
        if (re.search(rb"/Width\s+%d\b" % width, window)
                and re.search(rb"/Height\s+%d\b" % height, window)):
            return True
    return False


def test_an_embedded_screenshot_reaches_the_pdf(tmp_path):
    """Prove a screenshot in `images=` survives Chromium's print pipeline.

    The brief's suggested check — `len(with_images) > len(without)` — is not
    sound on its own: a larger PDF proves *some* bytes were added somewhere,
    not that an image XObject was rendered, and a probe against this exact
    pipeline showed it can point the wrong way entirely (see
    `_a_realistic_screenshot`). So the deciding assertion here is structural:
    the PDF rendered with `images=` must contain an `/Image` XObject whose
    `/Width`/`/Height` match what `embed_png` computed for this screenshot,
    and the PDF rendered without `images=` must not. That is direct evidence
    the specific screenshot — not an unrelated image — reached the print
    pipeline, which a byte count alone cannot distinguish from an unrelated
    change in page content (font subsetting, empty-state caption text, etc).

    The size comparison is kept as a secondary sanity check, satisfied here
    because the fixture image has real entropy — but it is not the check
    doing the proving.
    """
    screenshot = _a_realistic_screenshot(tmp_path / "shot.png")
    report = Report.model_validate(
        a_report(appendix=[an_appendix_entry(screenshot=str(screenshot))])
    )
    entry = report.appendix[0]
    embedded = embed_png(screenshot, width=720, max_height=1600, root=tmp_path)
    assert embedded is not None  # the fixture must actually embed, or the test proves nothing

    with_images = render_pdf(
        render_html(report, images={entry_key(entry): embedded}),
        page_factory=chromium_page_factory,
    )
    without_images = render_pdf(render_html(report), page_factory=chromium_page_factory)

    assert _pdf_has_image_xobject(with_images, width=embedded.width, height=embedded.height)
    assert not _pdf_has_image_xobject(
        without_images, width=embedded.width, height=embedded.height
    )
    # Secondary, weaker signal — true for this entropy-rich fixture, but not
    # by itself proof of anything (see docstring above).
    assert len(with_images) > len(without_images)
