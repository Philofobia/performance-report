"""Screenshots into the rendered document (PROJECT_SPEC §10 Phase 7B).

The report is delivered as a single self-contained HTML file, and
``render_pdf.py`` hands it to Chromium via ``set_content`` — no navigation, no
origin, nothing for a ``file://`` reference to resolve against. An embedded
screenshot therefore has to be a ``data:`` URI baked into the markup, exactly as
the charts are inline SVG.

Two constraints shape everything here:

* **Determinism.** The same source file must always produce the same data URI,
  or the project's "same data, same report" promise is false. The resampling
  filter is pinned and no metadata is written, which is the same lesson
  matplotlib's randomised element ids already taught.
* **Path confinement.** The path comes from ``report.json`` — a file on disk a
  user can hand-edit. A renderer that reads whatever path it is handed and
  base64s it into a shareable document is a file-disclosure primitive. Every
  path is resolved and checked against the artifacts root before it is opened.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from PIL import Image, UnidentifiedImageError

DATA_URI_PREFIX = "data:image/png;base64,"

#: Pinned so two renders of the same capture cannot differ. Any change here is
#: a deliberate visual change and will alter every embedded screenshot.
RESAMPLE = Image.LANCZOS


@dataclass(frozen=True)
class EmbeddedImage:
    """A screenshot ready for the template, plus what was done to it."""

    data_uri: str
    width: int
    height: int
    cropped: bool


def _within(path: Path, root: Path) -> bool:
    """True when ``path`` resolves inside ``root``."""
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def embed_png(
    path: str | Path, *, width: int, max_height: int, root: str | Path
) -> Optional[EmbeddedImage]:
    """Downscale a screenshot and return it as a data URI.

    Returns ``None`` for anything that cannot be embedded — missing file,
    undecodable bytes, or a path outside ``root``. The caller renders the
    figure's empty state; no capture is ever a reason to fail a render.
    """
    source, artifacts_root = Path(path), Path(root)
    if not _within(source, artifacts_root):
        return None

    try:
        with Image.open(source) as image:
            image.load()
            picture = image.convert("RGB")
    except (OSError, UnidentifiedImageError, ValueError,
            Image.DecompressionBombError):
        # DecompressionBombError subclasses Exception directly, not OSError,
        # and Image.open() raises it from the header alone — before .load()
        # runs — whenever declared width x height exceeds MAX_IMAGE_PIXELS.
        # A full-page desktop capture of a long page (e.g. 1920x47000) clears
        # that bound routinely; it is a bad capture, not an attack, and must
        # not take the whole render down.
        return None

    original_width, original_height = picture.size
    if original_width <= 0 or original_height <= 0:
        return None

    # Never upscale: enlarging a 320px capture to 720px invents detail.
    target_width = min(int(width), original_width)
    target_height = max(1, round(original_height * target_width / original_width))
    picture = picture.resize((target_width, target_height), RESAMPLE)

    cropped = target_height > int(max_height)
    if cropped:
        picture = picture.crop((0, 0, target_width, int(max_height)))
        target_height = int(max_height)

    buffer = io.BytesIO()
    # No `pnginfo`: Pillow would otherwise be free to carry source metadata
    # through, and a timestamp in the payload breaks byte-identical re-renders.
    picture.save(buffer, format="PNG", optimize=True)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")

    return EmbeddedImage(
        data_uri=DATA_URI_PREFIX + payload,
        width=target_width,
        height=target_height,
        cropped=cropped,
    )


#: ASCII Unit Separator. A non-printable control character, so it cannot
#: appear in a page name, run id, device, or network label written through
#: this pipeline's own config/YAML/JSON — those are all human- or
#: slug-authored text. Joining on it therefore can't fold two distinct
#: four-part identities into the same string.
_KEY_SEP = "\x1f"


def entry_key(entry) -> str:
    """The identity of one appendix entry.

    `run_id` alone is not unique — runs loaded straight from `data/processed`
    carry no uniqueness constraint, which is why the appendix sorts on this
    same four-part tuple. Keying the image map by `run_id` would let one
    capture's screenshot overwrite another's, and a report showing the wrong
    screenshot is worse than one showing no screenshot at all.
    """
    return _KEY_SEP.join((entry.page, entry.run_id, entry.device, entry.network))


def build_appendix_images(
    report, *, root: str | Path, width: int, max_height: int
) -> Dict[str, EmbeddedImage]:
    """Embed every appendix screenshot, keyed by :func:`entry_key`.

    `run_id` alone would collide (see `entry_key`), so the map key is the same
    four-part identity the appendix itself sorts on — the template must use
    `entry_key` to look entries up, or the two will disagree about identity.

    Entries that cannot be embedded are simply absent from the mapping, which
    is what the template reads as "render the empty state".
    """
    images: Dict[str, EmbeddedImage] = {}
    for entry in report.appendix:
        if not entry.screenshot:
            continue
        embedded = embed_png(
            entry.screenshot, width=width, max_height=max_height, root=root
        )
        if embedded is not None:
            images[entry_key(entry)] = embedded
    return images
