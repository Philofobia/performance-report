"""Unit tests for report/images.py — screenshots into the rendered document.

Two properties matter here and neither is cosmetic. The encoder must be
deterministic, or "same data, same report" is false. And the path must be
confined to the artifacts root, because it arrives from a JSON file on disk
that anyone can hand-edit — an unconfined reader is a file-disclosure
primitive wearing a report renderer's clothes.
"""
from __future__ import annotations

import base64
import re

from PIL import Image

from report.images import (
    DATA_URI_PREFIX,
    build_appendix_images,
    embed_png,
    entry_key,
)


def a_png(tmp_path, *, size=(1440, 900), name="screenshot.png"):
    path = tmp_path / name
    Image.new("RGB", size, (10, 120, 200)).save(path, format="PNG")
    return path


class _Entry:
    """Duck-typed stand-in for `AppendixEntry` — only the fields images.py reads."""

    def __init__(self, *, page, run_id, device, network, screenshot):
        self.page = page
        self.run_id = run_id
        self.device = device
        self.network = network
        self.screenshot = screenshot


class _Report:
    """Duck-typed stand-in for `Report` — only the field images.py reads."""

    def __init__(self, appendix):
        self.appendix = appendix


def test_a_screenshot_is_downscaled_to_the_configured_width(tmp_path):
    result = embed_png(a_png(tmp_path, size=(1440, 900)),
                       width=720, max_height=1600, root=tmp_path)
    assert result.width == 720
    assert result.height == 450  # aspect ratio preserved
    assert result.cropped is False


def test_an_image_narrower_than_the_target_is_not_upscaled(tmp_path):
    result = embed_png(a_png(tmp_path, size=(320, 200)),
                       width=720, max_height=1600, root=tmp_path)
    assert result.width == 320


def test_a_full_page_capture_is_cropped_from_the_top_and_says_so(tmp_path):
    # A full-page mobile capture runs to tens of thousands of pixels. Scaling
    # it to fit would render an unreadable smear that looks like a broken file.
    result = embed_png(a_png(tmp_path, size=(720, 20000)),
                       width=720, max_height=1600, root=tmp_path)
    assert result.height == 1600
    assert result.cropped is True


def test_the_data_uri_decodes_to_a_png(tmp_path):
    result = embed_png(a_png(tmp_path), width=720, max_height=1600, root=tmp_path)
    assert result.data_uri.startswith(DATA_URI_PREFIX)
    raw = base64.b64decode(result.data_uri[len(DATA_URI_PREFIX):])
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")


def test_the_payload_is_base64_only_before_it_is_marked_safe(tmp_path):
    result = embed_png(a_png(tmp_path), width=720, max_height=1600, root=tmp_path)
    payload = result.data_uri[len(DATA_URI_PREFIX):]
    assert re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", payload)


def test_encoding_the_same_file_twice_produces_identical_bytes(tmp_path):
    path = a_png(tmp_path)
    first = embed_png(path, width=720, max_height=1600, root=tmp_path)
    second = embed_png(path, width=720, max_height=1600, root=tmp_path)
    assert first.data_uri == second.data_uri


def test_a_file_that_is_not_an_image_returns_none(tmp_path):
    path = tmp_path / "screenshot.png"
    path.write_text("this is not a PNG", encoding="utf-8")
    assert embed_png(path, width=720, max_height=1600, root=tmp_path) is None


def test_a_missing_file_returns_none(tmp_path):
    assert embed_png(tmp_path / "gone.png", width=720, max_height=1600,
                     root=tmp_path) is None


def test_a_path_outside_the_artifacts_root_is_refused(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    secret = a_png(outside, name="secret.png")
    root = tmp_path / "raw"
    root.mkdir()
    assert embed_png(secret, width=720, max_height=1600, root=root) is None


def test_a_traversal_path_is_refused(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    a_png(tmp_path, name="secret.png")
    assert embed_png(root / ".." / "secret.png", width=720, max_height=1600,
                     root=root) is None


def test_a_decompression_bomb_header_returns_none_instead_of_raising(
    tmp_path, monkeypatch
):
    # Image.open() raises DecompressionBombError from the header alone, before
    # .load() runs, whenever declared width x height clears MAX_IMAGE_PIXELS.
    # It subclasses Exception directly, not OSError — a naive except clause
    # lets it escape and take the whole report render down. Lower the limit
    # rather than generate a genuinely huge file: an ordinary capture crosses
    # a lowered limit exactly as a huge one crosses the real one.
    path = a_png(tmp_path, size=(1440, 900))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    assert embed_png(path, width=720, max_height=1600, root=tmp_path) is None


def test_build_appendix_images_does_not_let_a_repeated_run_id_collide(tmp_path):
    # run_id is not unique across appendix entries on the load_runs path
    # (analysis/reportmodel.py:439-441) — two conditions of the same run can
    # share one. Keying on run_id alone would let the second screenshot
    # silently overwrite the first.
    mobile_shot = a_png(tmp_path, size=(1440, 900), name="mobile.png")
    desktop_shot = a_png(tmp_path, size=(200, 100), name="desktop.png")
    mobile = _Entry(page="home", run_id="run-1", device="mobile",
                     network="slow-4g", screenshot=str(mobile_shot))
    desktop = _Entry(page="home", run_id="run-1", device="desktop",
                      network="fast-3g", screenshot=str(desktop_shot))
    report = _Report([mobile, desktop])

    images = build_appendix_images(report, root=tmp_path, width=720, max_height=1600)

    assert len(images) == 2
    mobile_key, desktop_key = entry_key(mobile), entry_key(desktop)
    assert mobile_key != desktop_key
    assert images[mobile_key].width == 720
    assert images[desktop_key].width == 200
