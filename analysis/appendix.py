"""HAR reduction for the report appendix (PROJECT_SPEC §10 Phase 7B).

A HAR is a multi-megabyte transcript of every request the page made. Embedding
it in a PDF is meaningless; the question a reader actually has is "what was
heavy", and that is a short table sorted by transfer size.

The reduction is pure — it takes a parsed HAR dict and returns rows. Only
:func:`read_har` and :func:`summarize_capture` touch the filesystem, and
neither raises: a campaign whose ``data/raw`` was cleaned three months ago must
still re-analyse and still produce a complete report.

**The input is the scrubbed HAR** written by ``store/artifacts.py``, so
credentials are already redacted. URLs are re-passed through
:func:`store.artifacts.redact_url` anyway: a HAR written before a scrubbing
rule existed is still sitting in the store, and this is the layer where it
becomes a rendered document.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from store.artifacts import redact_url

#: Playwright's own `_resourceType` vocabulary, kept verbatim rather than
#: mapped onto the `initiatorType` values in `PageBlock.resource_type_totals`.
#: The two come from different browser APIs; renaming one to match the other
#: would imply the tables are the same taxonomy when they are not.
CANONICAL_TYPES: Tuple[str, ...] = (
    "document", "stylesheet", "script", "image", "font", "media", "xhr", "other",
)

_MIME_PREFIXES = (
    ("text/html", "document"),
    ("text/css", "stylesheet"),
    ("image/", "image"),
    ("font/", "font"),
    ("audio/", "media"),
    ("video/", "media"),
    ("application/javascript", "script"),
    ("text/javascript", "script"),
    ("application/json", "xhr"),
    ("application/font", "font"),
)

_EXTENSIONS = {
    ".html": "document", ".htm": "document",
    ".css": "stylesheet",
    ".js": "script", ".mjs": "script",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".avif": "image", ".svg": "image", ".ico": "image",
    ".woff": "font", ".woff2": "font", ".ttf": "font", ".otf": "font",
    ".mp4": "media", ".webm": "media", ".mp3": "media",
    ".json": "xhr",
}


@dataclass(frozen=True)
class HarSummary:
    """The appendix's view of one capture's requests."""

    rows: List[Dict[str, Any]]
    total_requests: int
    total_transfer_bytes: int


@dataclass(frozen=True)
class CaptureSummary:
    """One appendix entry's derived data, degradation included."""

    har_sha256: Optional[str] = None
    har_bytes: Optional[int] = None
    requests: List[Dict[str, Any]] = field(default_factory=list)
    total_requests: int = 0
    total_transfer_bytes: int = 0
    degraded: List[str] = field(default_factory=list)


def _response(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    response = entry.get("response")
    return response if isinstance(response, Mapping) else {}


def _url(entry: Mapping[str, Any]) -> str:
    request = entry.get("request")
    url = request.get("url") if isinstance(request, Mapping) else None
    return str(url or "")


def classify(entry: Mapping[str, Any]) -> str:
    """The resource type for one HAR entry.

    Playwright's label first, then the response mime type, then the URL
    extension. Every branch is a pure lookup, so the same HAR always classifies
    the same way — a classifier that guessed differently between runs would
    reorder nothing but would still make two renders disagree.
    """
    label = str(entry.get("_resourceType") or "").lower()
    if label in CANONICAL_TYPES:
        return label
    if label in ("fetch", "xhr"):
        return "xhr"
    if label == "img":
        return "image"

    content = _response(entry).get("content")
    mime_value = content.get("mimeType") if isinstance(content, Mapping) else None
    mime = str(mime_value or "").lower()
    for prefix, kind in _MIME_PREFIXES:
        if mime.startswith(prefix):
            return kind

    suffix = Path(urlsplit(_url(entry)).path).suffix.lower()
    return _EXTENSIONS.get(suffix, "other")


def entry_transfer_bytes(entry: Mapping[str, Any]) -> int:
    """Bytes on the wire for one entry, never negative.

    ``_transferSize`` is authoritative when present. A response served from
    cache reports ``-1``, and a negative number in a size table is worse than a
    zero — it sorts to the bottom and reads as corrupt data.
    """
    response = _response(entry)
    size = response.get("_transferSize")
    if isinstance(size, (int, float)):
        return max(0, int(size))

    body = response.get("bodySize")
    headers = response.get("headersSize")
    total = 0
    for part in (body, headers):
        if isinstance(part, (int, float)) and part > 0:
            total += int(part)
    return total


def _row(entry: Mapping[str, Any]) -> Dict[str, Any]:
    response = _response(entry)
    status = response.get("status")
    time = entry.get("time")
    return {
        "url": redact_url(_url(entry)),
        "resource_type": classify(entry),
        "status": int(status) if isinstance(status, (int, float)) else None,
        "transfer_bytes": entry_transfer_bytes(entry),
        # Absent, not zero: the run listing already established that a missing
        # measurement must never read as a perfect one.
        "duration_ms": round(float(time), 3) if isinstance(time, (int, float)) else None,
    }


def reduce_har(har: Mapping[str, Any], *, top_n: int = 15) -> HarSummary:
    """Reduce a parsed HAR to its heaviest ``top_n`` requests plus true totals.

    ``total_requests`` accompanies the truncated rows deliberately. A table of
    15 rows summing to 2 MB, with nothing saying the page made 214 requests
    totalling 8 MB, is a misleading document.
    """
    log = har.get("log") if isinstance(har, Mapping) else None
    entries = log.get("entries") if isinstance(log, Mapping) else None
    if not isinstance(entries, list):
        entries = []

    rows = [_row(e) for e in entries if isinstance(e, Mapping)]
    # The URL tie-break is what makes the order reproducible: identically-sized
    # responses (empty 204s, sprites from one build) are common, and without it
    # their order comes from input order and two renders can disagree. The
    # original index is a third, self-contained component: two entries that
    # tie on both size and URL (a tracking pixel fired twice) still need a
    # total order, and the index is the one thing about them that is never
    # itself equal.
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda pair: (-pair[1]["transfer_bytes"], pair[1]["url"], pair[0]))
    rows = [row for _, row in indexed]
    return HarSummary(
        rows=rows[: max(0, int(top_n))],
        total_requests=len(rows),
        total_transfer_bytes=sum(r["transfer_bytes"] for r in rows),
    )


def read_har(path: str | Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read and parse a HAR, returning ``(har, error)`` rather than raising.

    The caller is assembling a report that must be produced regardless, so a
    truncated capture is a fact to record, not an exception to propagate.
    """
    p = Path(path)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"HAR file not found: {p.name}"
    except OSError as exc:
        return None, f"HAR unreadable: {exc.strerror or exc}"
    except json.JSONDecodeError as exc:
        return None, f"HAR malformed: {exc}"
    if not isinstance(payload, dict):
        return None, "HAR malformed: top level is not an object"
    return payload, None


def summarize_capture(
    *, screenshot: Optional[str], har: Optional[str], top_n: int
) -> CaptureSummary:
    """Derive one appendix entry's data, degrading per artifact.

    Screenshot handling here is a *stat only* — whether the file exists. Whether
    it decodes is discoverable only by decoding it, which happens in the report
    layer at render time, and the report layer must never reach back and edit
    ``report.json`` to record what it found.
    """
    degraded: List[str] = []

    if not screenshot:
        degraded.append("screenshot not retained")
    elif not Path(screenshot).is_file():
        degraded.append("screenshot file missing")

    if not har:
        degraded.append("HAR not retained")
        return CaptureSummary(degraded=degraded)

    har_path = Path(har)
    if not har_path.is_file():
        degraded.append("HAR file missing")
        return CaptureSummary(degraded=degraded)

    raw = har_path.read_bytes()
    payload, error = read_har(har_path)
    if payload is None:
        degraded.append(error or "HAR unreadable")
        return CaptureSummary(
            har_sha256=hashlib.sha256(raw).hexdigest(),
            har_bytes=len(raw),
            degraded=degraded,
        )

    summary = reduce_har(payload, top_n=top_n)
    return CaptureSummary(
        har_sha256=hashlib.sha256(raw).hexdigest(),
        har_bytes=len(raw),
        requests=summary.rows,
        total_requests=summary.total_requests,
        total_transfer_bytes=summary.total_transfer_bytes,
        degraded=degraded,
    )
