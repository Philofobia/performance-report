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

from report.images import DATA_URI_PREFIX, embed_png


def a_png(tmp_path, *, size=(1440, 900), name="screenshot.png"):
    path = tmp_path / name
    Image.new("RGB", size, (10, 120, 200)).save(path, format="PNG")
    return path


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
