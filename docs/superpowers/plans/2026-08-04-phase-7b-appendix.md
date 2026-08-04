# Phase 7B — Screenshot / HAR Appendix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed each capture's screenshot and a top-N request table derived from its scrubbed HAR into the report, as a new fixed `appendix` section.

**Architecture:** `analysis/appendix.py` reduces a scrubbed HAR to request rows and writes them into `report.json`; `report/images.py` turns a screenshot path into a base64 `data:` URI at render time. The split follows the project rule that analysis is the last stage that computes anything and the report layer only formats. `report/skeleton.py:collapse()` is generalized from one repeating root to N so the per-capture block stays guarded by `--skeleton-check`.

**Tech Stack:** Python 3.11+, Pydantic v2, Jinja2, Pillow (already installed as a matplotlib dependency), pytest.

**Design spec:** `docs/superpowers/specs/2026-08-04-phase-7b-appendix-design.md`

## Global Constraints

- **Determinism is the headline promise.** Same inputs must produce byte-identical HTML. Any new ordering needs an explicit tie-break; any new encoder must be pinned (no timestamps, no random ids).
- **No section is ever conditionally omitted.** Missing data renders an explicit empty state, never a vanished block. This applies to the new sub-blocks too.
- **The report layer computes nothing.** Every number in the appendix table is decided in `analysis/` and read out of `report.json`.
- **`|safe` is confined to values this codebase generated.** Never model prose, never a value read from `report.json` that originated outside our code.
- **A metric or value that does not exist prints `—`, never `0`.**
- **Tests are offline by default.** No browser, no network, outside `-m e2e`. Run `pytest -m "not e2e"`.
- **Coverage floor is 80%**, enforced in CI.
- **Analysis never fails over an unavailable artifact.** A campaign whose `data/raw` was cleaned still produces a complete report.
- Existing test file naming is `tests/unit/<module>_test.py` (suffix, not prefix).

---

## File Structure

**Create:**
- `analysis/appendix.py` — HAR reduction to request rows. Pure reduction plus one thin file-read helper.
- `report/images.py` — PNG → downscaled base64 data URI, with path confinement.
- `tests/unit/appendix_test.py`
- `tests/unit/images_test.py`

**Modify:**
- `analysis/reportmodel.py` — `RequestRow`, `AppendixEntry`, `Report.appendix`, `ReportMeta.degraded_appendix_entries`, `_appendix()`, `SCHEMA_VERSION` 1 → 2
- `report/skeleton.py` — `collapse()` generalized to N group roots
- `report/skeleton.baseline.json` — three added entries
- `report/render_html.py` — `images` parameter
- `report/render_md.py` — `base_dir` parameter, `MD_SECTIONS` gains `Appendix`
- `report/template/report.html.j2` — the appendix section
- `report/template/report.md.j2` — the appendix section
- `report/template/style.css` — appendix figure and table styling
- `report/__main__.py` — `--no-appendix-images`, image building in `write_outputs`
- `config/load.py` — `AppendixConfig`, `ReportConfig.appendix`
- `config/settings.yaml` — the three appendix keys
- `requirements.txt` — explicit `pillow` pin
- `tests/unit/reportmodel_test.py`, `tests/unit/skeleton_test.py`, `tests/unit/render_html_test.py`, `tests/unit/render_md_test.py`, `tests/integration/` — extended
- `README.md`, `docs/PROJECT_SPEC.md`, `docs/SECURITY_PLAN.md`

---

### Task 1: HAR reduction

**Files:**
- Create: `analysis/appendix.py`
- Test: `tests/unit/appendix_test.py`

**Interfaces:**
- Consumes: `store.artifacts.redact_url` (existing, `store/artifacts.py:86`)
- Produces:
  - `CANONICAL_TYPES: Tuple[str, ...]`
  - `classify(entry: Mapping[str, Any]) -> str`
  - `entry_transfer_bytes(entry: Mapping[str, Any]) -> int`
  - `@dataclass(frozen=True) HarSummary(rows: List[Dict[str, Any]], total_requests: int, total_transfer_bytes: int)`
  - `reduce_har(har: Mapping[str, Any], *, top_n: int = 15) -> HarSummary`
  - `read_har(path: str | Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]` — `(har, error_message)`
  - `@dataclass(frozen=True) CaptureSummary(har_sha256, har_bytes, requests, total_requests, total_transfer_bytes, degraded)`
  - `summarize_capture(*, screenshot: Optional[str], har: Optional[str], top_n: int) -> CaptureSummary`

`rows` entries are plain dicts with keys `url`, `resource_type`, `status`, `transfer_bytes`, `duration_ms`. They are **not** `RequestRow` instances: `reportmodel` imports this module, so importing `reportmodel` back would be circular. This mirrors how `analysis/trends.py` returns plain series that `TrendSeriesModel.of()` converts.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/appendix_test.py`:

```python
"""Unit tests for analysis/appendix.py — HAR reduction for the report appendix.

The reduction is what makes a multi-megabyte HAR into a table a reader can act
on, so the tests are about honesty: the top-N must be the actual top N, the
totals must describe the whole capture rather than the slice, and a HAR the
browser truncated must degrade rather than abort the report.
"""
from __future__ import annotations

import json

from analysis.appendix import (
    HarSummary,
    classify,
    entry_transfer_bytes,
    read_har,
    reduce_har,
    summarize_capture,
)


def an_entry(url="https://example.com/a.js", *, size=1000, mime="text/javascript",
             resource_type=None, status=200, time=12.5):
    entry = {
        "time": time,
        "request": {"url": url},
        "response": {
            "status": status,
            "_transferSize": size,
            "content": {"mimeType": mime},
        },
    }
    if resource_type is not None:
        entry["_resourceType"] = resource_type
    return entry


def a_har(entries):
    return {"log": {"version": "1.2", "entries": list(entries)}}


def test_rows_are_ordered_by_transfer_size_descending():
    har = a_har([
        an_entry("https://example.com/small.js", size=10),
        an_entry("https://example.com/huge.js", size=9000),
        an_entry("https://example.com/mid.js", size=500),
    ])
    rows = reduce_har(har, top_n=10).rows
    assert [r["url"] for r in rows] == [
        "https://example.com/huge.js",
        "https://example.com/mid.js",
        "https://example.com/small.js",
    ]


def test_equal_sizes_are_tie_broken_by_url_so_order_is_reproducible():
    har = a_har([
        an_entry("https://example.com/z.js", size=500),
        an_entry("https://example.com/a.js", size=500),
    ])
    rows = reduce_har(har, top_n=10).rows
    assert [r["url"] for r in rows] == [
        "https://example.com/a.js",
        "https://example.com/z.js",
    ]


def test_top_n_truncates_but_totals_describe_the_whole_capture():
    har = a_har([an_entry(f"https://example.com/{i}.js", size=100) for i in range(20)])
    summary = reduce_har(har, top_n=5)
    assert len(summary.rows) == 5
    assert summary.total_requests == 20
    assert summary.total_transfer_bytes == 2000


def test_negative_transfer_size_from_a_cache_hit_clamps_to_zero():
    # A HAR reports -1 for a served-from-cache response. A negative byte count
    # in a size table is worse than a zero.
    assert entry_transfer_bytes(an_entry(size=-1)) == 0


def test_transfer_size_falls_back_to_body_plus_headers():
    entry = {"request": {"url": "https://example.com/a.js"},
             "response": {"bodySize": 900, "headersSize": 100}}
    assert entry_transfer_bytes(entry) == 1000


def test_resource_type_prefers_playwrights_own_label():
    assert classify(an_entry(resource_type="stylesheet", mime="text/plain")) == "stylesheet"


def test_resource_type_falls_back_to_mime_then_extension_then_other():
    assert classify(an_entry(mime="image/png")) == "image"
    assert classify({"request": {"url": "https://example.com/x.woff2"},
                     "response": {}}) == "font"
    assert classify({"request": {"url": "https://example.com/x"},
                     "response": {}}) == "other"


def test_urls_are_re_redacted_even_though_the_stored_har_was_scrubbed():
    # A HAR written before a scrubbing rule existed is still in the store.
    har = a_har([an_entry("https://example.com/a.js?token=hunter2")])
    assert "hunter2" not in reduce_har(har, top_n=5).rows[0]["url"]


def test_missing_duration_is_none_not_zero():
    entry = {"request": {"url": "https://example.com/a.js"}, "response": {}}
    assert reduce_har(a_har([entry]), top_n=5).rows[0]["duration_ms"] is None


def test_a_har_with_no_entries_reduces_to_an_empty_summary():
    assert reduce_har(a_har([]), top_n=5) == HarSummary(
        rows=[], total_requests=0, total_transfer_bytes=0
    )


def test_a_har_missing_its_log_block_reduces_to_an_empty_summary():
    assert reduce_har({"not": "a har"}, top_n=5).total_requests == 0


def test_read_har_reports_a_missing_file_as_an_error_not_an_exception(tmp_path):
    har, error = read_har(tmp_path / "gone.har")
    assert har is None
    assert "not found" in error.lower()


def test_read_har_reports_malformed_json_with_the_parser_detail(tmp_path):
    path = tmp_path / "capture.har"
    path.write_text("{ truncated", encoding="utf-8")
    har, error = read_har(path)
    assert har is None
    assert "malformed" in error.lower()


def test_summarize_capture_records_the_har_digest_and_size(tmp_path):
    path = tmp_path / "capture.har"
    payload = json.dumps(a_har([an_entry(size=42)]))
    path.write_text(payload, encoding="utf-8")

    summary = summarize_capture(screenshot=None, har=str(path), top_n=5)
    assert summary.har_bytes == len(payload.encode("utf-8"))
    assert len(summary.har_sha256) == 64
    assert summary.total_transfer_bytes == 42


def test_summarize_capture_degrades_per_artifact_without_raising(tmp_path):
    summary = summarize_capture(screenshot=None, har=None, top_n=5)
    assert summary.degraded == ["screenshot not retained", "HAR not retained"]
    assert summary.requests == []


def test_summarize_capture_reports_a_path_that_points_at_nothing(tmp_path):
    summary = summarize_capture(
        screenshot=str(tmp_path / "gone.png"), har=str(tmp_path / "gone.har"), top_n=5
    )
    assert summary.degraded == ["screenshot file missing", "HAR file missing"]


def test_summarize_capture_reports_a_malformed_har_and_still_returns(tmp_path):
    path = tmp_path / "capture.har"
    path.write_text("{ truncated", encoding="utf-8")
    summary = summarize_capture(screenshot=None, har=str(path), top_n=5)
    assert any("malformed" in reason.lower() for reason in summary.degraded)
    assert summary.requests == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/appendix_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analysis.appendix'`

- [ ] **Step 3: Write the implementation**

Create `analysis/appendix.py`:

```python
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

    mime = str(_response(entry).get("content", {}).get("mimeType") or "").lower()
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
    # their order comes from input order and two renders can disagree.
    rows.sort(key=lambda r: (-r["transfer_bytes"], r["url"]))
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/appendix_test.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 5: Commit**

```bash
git add analysis/appendix.py tests/unit/appendix_test.py
git commit -m "Reduce a captured HAR to the requests that made the page heavy"
```

---

### Task 2: Report JSON contract

**Files:**
- Modify: `analysis/reportmodel.py` (`SCHEMA_VERSION` at line 26, `CaptureRow` at 212, `Methodology` at 220, `ReportMeta` at 228, `Report` at 237, `_methodology` at 349, `build_report` at 372)
- Modify: `config/load.py:225-227`, `config/settings.yaml`
- Test: `tests/unit/reportmodel_test.py`, `tests/unit/config_test.py`

**Interfaces:**
- Consumes: `analysis.appendix.summarize_capture`, `analysis.appendix.CaptureSummary` (Task 1)
- Produces:
  - `RequestRow(url, resource_type, status, transfer_bytes, duration_ms)`
  - `AppendixEntry(page, run_id, device, network, screenshot, har, har_sha256, har_bytes, requests, total_requests, total_transfer_bytes, degraded)`
  - `Report.appendix: List[AppendixEntry]`
  - `ReportMeta.degraded_appendix_entries: int`
  - `_appendix(pages: Sequence[PageAnalysis], settings: Settings) -> List[AppendixEntry]`
  - `AppendixConfig(top_requests=15, screenshot_width_px=720, screenshot_max_height_px=1600)`
  - `ReportConfig.appendix: AppendixConfig`

`methodology.captures` is deliberately left unchanged — see design §4.2.

**The config lands here, not in Task 7**, because `_appendix` reads
`settings.report.appendix.top_requests` — without it this task cannot run.
Task 7 wires only the CLI and the dependency pin.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/reportmodel_test.py`:

```python
def test_appendix_carries_one_entry_per_capture_ordered_by_page_and_run():
    report = a_report_with_captures(pages=("plp", "homepage"))
    assert [(e.page, e.run_id) for e in report.appendix] == [
        ("homepage", "run_homepage"), ("plp", "run_plp"),
    ]


def test_appendix_entries_carry_the_condition_they_were_captured_under():
    entry = a_report_with_captures().appendix[0]
    assert (entry.device, entry.network) == ("mid-mobile", "slow-4g")


def test_appendix_holds_paths_never_image_bytes():
    # report.json must stay a text document a human can read and git can diff.
    payload = to_json(a_report_with_captures())
    assert "base64" not in payload
    assert "data:image" not in payload


def test_appendix_rows_come_from_the_har(tmp_path):
    report = a_report_with_captures(har=a_har_file(tmp_path, sizes=(9000, 10)))
    entry = report.appendix[0]
    assert [r.transfer_bytes for r in entry.requests] == [9000, 10]
    assert entry.total_requests == 2
    assert entry.total_transfer_bytes == 9010


def test_a_capture_with_no_artifacts_degrades_without_dropping_the_entry():
    report = a_report_with_captures(screenshot=None, har=None)
    assert len(report.appendix) == 1
    assert report.appendix[0].degraded == [
        "screenshot not retained", "HAR not retained",
    ]


def test_degraded_entries_are_counted_in_meta():
    report = a_report_with_captures(screenshot=None, har=None)
    assert report.meta.degraded_appendix_entries == 1


def test_a_clean_capture_counts_as_zero_degraded(tmp_path):
    report = a_report_with_captures(
        screenshot=a_png_file(tmp_path), har=a_har_file(tmp_path)
    )
    assert report.appendix[0].degraded == []
    assert report.meta.degraded_appendix_entries == 0


def test_methodology_captures_are_unchanged_by_the_appendix():
    report = a_report_with_captures()
    assert [c.run_id for c in report.methodology.captures] == ["run_homepage"]


def test_schema_version_records_the_appendix_addition():
    assert a_report_with_captures().schema_version == 2
```

Add these helpers near the top of the file, beside the existing fixtures:

```python
def a_png_file(tmp_path, name="screenshot.png"):
    """A real 2x2 PNG on disk — Pillow writes it so the bytes are valid."""
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (2, 2), (255, 0, 0)).save(path, format="PNG")
    return str(path)


def a_har_file(tmp_path, *, sizes=(1000,), name="capture.har"):
    import json as _json

    entries = [
        {"time": 10.0,
         "request": {"url": f"https://example.com/{i}.js"},
         "response": {"status": 200, "_transferSize": size,
                      "content": {"mimeType": "text/javascript"}}}
        for i, size in enumerate(sizes)
    ]
    path = tmp_path / name
    path.write_text(_json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return str(path)
```

`a_report_with_captures(...)` builds a `Report` through `build_report` with `run.captures.screenshot` / `.har` set to the given paths — follow the existing `a_report(...)` helper in this file and pass the capture paths through the same `PageAnalysis` construction it already uses.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/reportmodel_test.py -k appendix -v`
Expected: FAIL — `AttributeError: 'Report' object has no attribute 'appendix'`

- [ ] **Step 3: Write the implementation**

In `analysis/reportmodel.py`, bump the version constant at line 26:

```python
SCHEMA_VERSION = 2  # 2: adds Report.appendix (Phase 7B)
```

Add the models next to `CaptureRow` (line 212):

```python
class RequestRow(BaseModel):
    """One request from a capture's HAR, as the appendix table shows it."""

    url: str
    resource_type: str = "other"
    status: Optional[int] = None
    transfer_bytes: int = 0
    duration_ms: Optional[float] = None


class AppendixEntry(BaseModel):
    """One capture's evidence: the screenshot path and its heaviest requests.

    ``screenshot`` is a path, never bytes. Embedding base64 here would cost the
    thing report.json exists for — a text document a reviewer can read and a
    diff can compare.

    ``total_requests`` sits beside the truncated ``requests`` list so the top-N
    can never read as the whole capture.
    """

    page: str
    run_id: str
    device: str
    network: str
    screenshot: Optional[str] = None
    har: Optional[str] = None
    har_sha256: Optional[str] = None
    har_bytes: Optional[int] = None
    requests: List[RequestRow] = Field(default_factory=list)
    total_requests: int = 0
    total_transfer_bytes: int = 0
    degraded: List[str] = Field(default_factory=list)
```

Add the field to `ReportMeta` (line 228):

```python
    #: Entries whose artifacts were missing or malformed *at analysis time*. A
    #: screenshot that exists but will not decode is only discoverable when
    #: something decodes it, which is render time, so it is not counted here.
    degraded_appendix_entries: int = 0
```

Add the field to `Report` (line 237), after `methodology`:

```python
    appendix: List[AppendixEntry] = Field(default_factory=list)
```

In `config/load.py`, replace `ReportConfig` (lines 225-227):

```python
class AppendixConfig(BaseModel):
    """The capture appendix (PROJECT_SPEC §10 Phase 7B).

    ``screenshot_max_height_px`` exists because a full-page mobile capture runs
    to tens of thousands of pixels. Scaling one to fit a page renders an
    unreadable smear, so beyond this height the image is cropped from the top
    and the crop is stated in the caption.
    """

    top_requests: int = Field(default=15, ge=1)
    screenshot_width_px: int = Field(default=720, ge=64)
    screenshot_max_height_px: int = Field(default=1600, ge=64)


class ReportConfig(BaseModel):
    output_dir: str = "data/reports"
    appendix: AppendixConfig = Field(default_factory=AppendixConfig)
```

In `config/settings.yaml`, extend the `report` block:

```yaml
report:
  output_dir: data/reports
  appendix:
    # How many requests the per-capture table shows, largest transfer first.
    # The true total is always stated beside the table, so this truncates the
    # listing without ever misrepresenting the capture.
    top_requests: 15
    # Screenshots are downscaled before embedding; a full-page mobile capture
    # is otherwise several MB of base64 per entry.
    screenshot_width_px: 720
    # Beyond this height the image is cropped from the top, and the caption
    # says so.
    screenshot_max_height_px: 1600
```

Add these tests to `tests/unit/config_test.py`:

```python
def test_appendix_settings_have_working_defaults():
    settings = Settings()
    assert settings.report.appendix.top_requests == 15
    assert settings.report.appendix.screenshot_width_px == 720
    assert settings.report.appendix.screenshot_max_height_px == 1600


def test_a_zero_top_requests_is_rejected_at_load_time():
    with pytest.raises(ValidationError):
        AppendixConfig(top_requests=0)


def test_the_shipped_settings_file_parses_its_appendix_block():
    settings = load_settings()
    assert settings.report.appendix.top_requests >= 1
```

Add the assembly function after `_methodology` (line 369):

```python
def _appendix(pages: Sequence[PageAnalysis], settings: Settings) -> List[AppendixEntry]:
    """One entry per capture, in the order methodology.captures uses.

    Never raises. A campaign whose raw artifacts were cleaned still analyses;
    the entries simply carry their degradation reasons.
    """
    from analysis import appendix as appendix_reduce

    top_n = settings.report.appendix.top_requests
    entries: List[AppendixEntry] = []
    for page in pages:
        for run in page.runs:
            summary = appendix_reduce.summarize_capture(
                screenshot=run.captures.screenshot,
                har=run.captures.har,
                top_n=top_n,
            )
            entries.append(AppendixEntry(
                page=page.page_name, run_id=run.run_id,
                device=run.condition.device, network=run.condition.network,
                screenshot=run.captures.screenshot, har=run.captures.har,
                har_sha256=summary.har_sha256, har_bytes=summary.har_bytes,
                requests=[RequestRow(**row) for row in summary.requests],
                total_requests=summary.total_requests,
                total_transfer_bytes=summary.total_transfer_bytes,
                degraded=list(summary.degraded),
            ))
    return sorted(entries, key=lambda e: (e.page, e.run_id))
```

In `build_report` (line 396), bind the entries before the `Report(...)` call and wire both fields:

```python
    appendix = _appendix(ordered, settings)
```

Then add `appendix=appendix,` after `methodology=_methodology(ordered, settings),`, and inside `ReportMeta(...)`:

```python
            degraded_appendix_entries=sum(1 for e in appendix if e.degraded),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/reportmodel_test.py -v`
Expected: PASS. Any existing test asserting `schema_version == 1` must be updated to `2` — that is the intended change, not a break.

- [ ] **Step 5: Commit**

```bash
git add analysis/reportmodel.py tests/unit/reportmodel_test.py
git commit -m "Carry per-capture evidence in the Report JSON"
```

---

### Task 3: Screenshot embedding

**Files:**
- Create: `report/images.py`
- Test: `tests/unit/images_test.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `DATA_URI_PREFIX: str = "data:image/png;base64,"`
  - `@dataclass(frozen=True) EmbeddedImage(data_uri: str, width: int, height: int, cropped: bool)`
  - `embed_png(path, *, width: int, max_height: int, root: str | Path) -> Optional[EmbeddedImage]`
  - `build_appendix_images(report, *, root, width, max_height) -> Dict[str, EmbeddedImage]` — keyed by `run_id`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/images_test.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/images_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'report.images'`

- [ ] **Step 3: Write the implementation**

Create `report/images.py`:

```python
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
from typing import Dict, Mapping, Optional

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
    except (OSError, UnidentifiedImageError, ValueError):
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


def build_appendix_images(
    report, *, root: str | Path, width: int, max_height: int
) -> Dict[str, EmbeddedImage]:
    """Embed every appendix screenshot, keyed by run id.

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
            images[entry.run_id] = embedded
    return images
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/images_test.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add report/images.py tests/unit/images_test.py
git commit -m "Embed screenshots as confined, deterministic data URIs"
```

---

### Task 4: Generalize the skeleton to N repeating roots

**Files:**
- Modify: `report/skeleton.py:31-34,61-86`
- Test: `tests/unit/skeleton_test.py`

**Interfaces:**
- Produces:
  - `GROUP_ROOTS: Tuple[str, ...] = ("page", "appendix")`
  - `collapse(sections: Sequence[str], *, roots: Sequence[str] = GROUP_ROOTS) -> List[str]`
  - `PAGE_GROUP` stays exported (existing tests import it)

`BASELINE_VERSION` stays `1`: a document with no appendix sections fingerprints identically under the generalized algorithm, so this cannot masquerade as drift.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/skeleton_test.py`:

```python
def appendix_entry():
    return (
        '<article data-section="appendix">'
        '<figure data-section="appendix.screenshot"></figure>'
        '<table data-section="appendix.requests"></table>'
        "</article>"
    )


def test_repeated_appendix_entries_collapse_to_one_group():
    sections = ["appendix", "appendix.screenshot", "appendix.requests"] * 4
    assert collapse(sections) == [
        "appendix[]", "appendix.screenshot", "appendix.requests",
    ]


def test_a_one_capture_and_a_six_capture_report_fingerprint_identically():
    # The same argument as one page versus three pages: the skeleton must be
    # independent of how much data the campaign happened to produce.
    def document_with(count):
        return (
            "<html><body>"
            + page_block("homepage")
            + '<section data-section="methodology"></section>'
            + '<section data-section="appendix">'
            + "".join(appendix_entry() for _ in range(count))
            + "</section></body></html>"
        )

    assert fingerprint(document_with(1)) == fingerprint(document_with(6))


def test_page_and_appendix_groups_both_collapse_in_one_document():
    sections = [
        "cover",
        "page", "page.header", "page", "page.header",
        "methodology",
        "appendix", "appendix.screenshot", "appendix", "appendix.screenshot",
    ]
    assert collapse(sections) == [
        "cover", "page[]", "page.header", "methodology",
        "appendix[]", "appendix.screenshot",
    ]


def test_a_document_without_an_appendix_fingerprints_exactly_as_before():
    # Proves the generalization is not itself a drift event.
    sections = ["cover", "page", "page.header", "page", "page.header", "methodology"]
    assert collapse(sections) == ["cover", "page[]", "page.header", "methodology"]


def test_a_group_root_with_no_children_is_still_emitted():
    assert collapse(["cover", "appendix", "appendix"]) == ["cover", "appendix[]"]
```

Add `GROUP_ROOTS` to the import block at the top of the file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/skeleton_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'GROUP_ROOTS'`

- [ ] **Step 3: Write the implementation**

Replace lines 31-34 of `report/skeleton.py`:

```python
#: Section roots whose blocks repeat once per data item. Each collapses to
#: ``<root>[]`` plus the children of its *first* occurrence, so the fingerprint
#: is independent of how many pages or captures the campaign produced.
GROUP_ROOTS: Tuple[str, ...] = ("page", "appendix")

PAGE_GROUP = "page[]"  # kept: the page group is named in tests and docs
```

Replace `collapse` (lines 61-86):

```python
def collapse(
    sections: Sequence[str], *, roots: Sequence[str] = GROUP_ROOTS
) -> List[str]:
    """Fold repeated per-item blocks into one ``<root>[]`` group each.

    Only the *first* block of each root contributes its children, so the result
    is independent of how many pages the campaign covered or how many captures
    it retained — which is the property that makes cross-campaign comparison
    meaningful.
    """
    out: List[str] = []
    emitted: set = set()
    index = 0
    while index < len(sections):
        section = sections[index]
        if section not in roots:
            out.append(section)
            index += 1
            continue

        prefix = f"{section}."
        block = [f"{section}[]"]
        index += 1
        while index < len(sections) and sections[index].startswith(prefix):
            block.append(sections[index])
            index += 1
        if section not in emitted:
            out.extend(block)
            emitted.add(section)
    return out
```

Add `Tuple` to the `typing` import on line 29.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/skeleton_test.py -v`
Expected: PASS. `test_the_committed_baseline_matches_the_real_template` still passes — the template has no appendix yet, so the fingerprint is unchanged.

- [ ] **Step 5: Commit**

```bash
git add report/skeleton.py tests/unit/skeleton_test.py
git commit -m "Let the skeleton guard more than one repeating block"
```

---

### Task 5: The HTML appendix section

**Files:**
- Modify: `report/template/report.html.j2:288` (after the methodology section), `report/template/style.css`, `report/render_html.py:93-101`, `report/skeleton.baseline.json`
- Test: `tests/unit/render_html_test.py`

**Interfaces:**
- Consumes: `report.images.EmbeddedImage`, `report.images.build_appendix_images` (Task 3); `Report.appendix` (Task 2)
- Produces: `render_html(report, *, images: Optional[Mapping[str, EmbeddedImage]] = None) -> str`

`images` defaults to `None` (path-only rows) so every existing caller and test keeps working unchanged, and so `--no-appendix-images` is expressed by simply not building the map.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/render_html_test.py`:

```python
def an_appendix_entry(run_id="run_homepage", *, screenshot="data/raw/s.png",
                      requests=True, degraded=()):
    return {
        "page": "homepage", "run_id": run_id,
        "device": "mid-mobile", "network": "slow-4g",
        "screenshot": screenshot, "har": "data/raw/capture.har",
        "har_sha256": "a" * 64, "har_bytes": 4096,
        "requests": [
            {"url": "https://example.com/hero.mp4", "resource_type": "media",
             "status": 200, "transfer_bytes": 4_200_000, "duration_ms": 3100.0},
        ] if requests else [],
        "total_requests": 214 if requests else 0,
        "total_transfer_bytes": 8_100_000 if requests else 0,
        "degraded": list(degraded),
    }


def test_the_appendix_renders_an_entry_per_capture():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    html = render_html(report)
    assert html.count('data-section="appendix.screenshot"') == 1
    assert html.count('data-section="appendix.requests"') == 1


def test_the_request_table_shows_the_url_and_its_transfer_size():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    html = render_html(report)
    assert "https://example.com/hero.mp4" in html
    assert "4.0 MB" in html  # 4_200_000 bytes / 1024 / 1024 = 4.005…


def test_the_true_request_count_is_stated_beside_the_truncated_table():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    assert "214" in render_html(report)


def test_a_screenshot_is_embedded_when_an_image_is_supplied():
    from report.images import EmbeddedImage

    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    html = render_html(report, images={
        "run_homepage": EmbeddedImage(data_uri="data:image/png;base64,AAAA",
                                      width=720, height=450, cropped=False),
    })
    assert "data:image/png;base64,AAAA" in html


def test_a_cropped_screenshot_says_so_in_the_caption():
    from report.images import EmbeddedImage

    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    html = render_html(report, images={
        "run_homepage": EmbeddedImage(data_uri="data:image/png;base64,AAAA",
                                      width=720, height=1600, cropped=True),
    })
    assert "top 1600" in html


def test_without_images_the_figure_renders_its_empty_state_not_a_broken_img():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    html = render_html(report)
    assert 'data-section="appendix.screenshot"' in html
    assert "data:image" not in html


def test_a_capture_with_no_requests_still_renders_the_table_block():
    report = Report.model_validate(
        a_report(appendix=[an_appendix_entry(requests=False,
                                             degraded=["HAR not retained"])])
    )
    html = render_html(report)
    assert 'data-section="appendix.requests"' in html
    assert "HAR not retained" in html


def test_an_empty_appendix_still_renders_the_section():
    # No section is ever conditionally omitted.
    html = render_html(Report.model_validate(a_report(appendix=[])))
    assert 'data-section="appendix"' in html


def test_a_one_capture_and_a_six_capture_report_share_a_fingerprint():
    one = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    six = Report.model_validate(a_report(appendix=[
        an_appendix_entry(run_id=f"run_{i}") for i in range(6)
    ]))
    assert fingerprint(render_html(one)) == fingerprint(render_html(six))
```

Extend the existing `a_report(...)` helper with an `appendix=()` keyword that lands in the returned dict as `"appendix": list(appendix)`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/render_html_test.py -k appendix -v`
Expected: FAIL — `TypeError: render_html() got an unexpected keyword argument 'images'`

- [ ] **Step 3: Write the implementation**

In `report/render_html.py`, add a byte formatter beside `metric_label` and register it:

```python
def transfer_size(value: Optional[float]) -> str:
    """Bytes as a short human string. Naming, not computing.

    A value the capture does not carry prints `—`, never `0`: a request with no
    recorded size is not a free request.
    """
    if value is None:
        return "—"
    size = float(value)
    for unit, scale in (("MB", 1024 * 1024), ("kB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{int(size)} B"
```

```python
    env.filters["transfer_size"] = transfer_size
```

Change `render_html`:

```python
def render_html(
    report: Report, *, images: Optional[Mapping[str, "EmbeddedImage"]] = None
) -> str:
    """Render the report to a single self-contained HTML document.

    ``images`` maps a run id to its embedded screenshot. It is built by the
    caller because embedding reads files, and this module is the one place the
    document is assembled — keeping the I/O outside it means a render is
    reproducible from its arguments alone. Omitting it renders the appendix
    with path-only rows, which is exactly what ``--no-appendix-images`` wants.
    """
    stylesheet = (TEMPLATE_DIR / STYLESHEET).read_text(encoding="utf-8")
    template = _env().get_template(HTML_TEMPLATE)
    return template.render(
        report=report,
        charts=build_charts(report),
        images=images or {},
        stylesheet=stylesheet,
    )
```

Add `Mapping` and `Optional` to the `typing` import, and import `EmbeddedImage` from `report.images` under `TYPE_CHECKING` to keep the annotation honest without a runtime cycle.

Append to `report/template/report.html.j2`, after the methodology section closes at line 288:

```jinja
<section data-section="appendix" class="sheet">
  <p class="eyebrow">Appendix</p>
  <h2>What was captured</h2>
  <p class="serif">
    One screenshot and the heaviest requests for each page and condition
    measured. Request tables are truncated to the largest transfers; the full
    count is stated beside each.
  </p>

  {% for entry in report.appendix %}
  <article data-section="appendix" class="capture">
    <h3>{{ entry.page }} <span class="cond">{{ entry.device }} · {{ entry.network }}</span></h3>
    <p class="mono meta-line">{{ entry.run_id }}</p>

    <figure data-section="appendix.screenshot" class="shot">
      {% if images.get(entry.run_id) %}
      {% set shot = images[entry.run_id] %}
      <img src="{{ shot.data_uri }}" width="{{ shot.width }}" height="{{ shot.height }}"
           alt="Screenshot of {{ entry.page }} on {{ entry.device }} over {{ entry.network }}">
      <figcaption>
        {% if shot.cropped %}
        Top {{ shot.height }} pixels of a full-page capture.
        {% else %}
        Full-page capture, scaled to {{ shot.width }} pixels wide.
        {% endif %}
      </figcaption>
      {% else %}
      <p class="empty">No screenshot was embedded for this capture.</p>
      <figcaption class="mono">{{ entry.screenshot or '—' }}</figcaption>
      {% endif %}
    </figure>

    <table data-section="appendix.requests" class="metrics requests">
      <caption>
        {% if entry.total_requests %}
        Heaviest {{ entry.requests|length }} of {{ entry.total_requests }} requests ·
        {{ entry.total_transfer_bytes|transfer_size }} transferred in total
        {% else %}
        No requests were available for this capture
        {% endif %}
      </caption>
      <thead>
        <tr><th>Request</th><th>Type</th><th>Status</th><th>Transfer</th><th>Time</th></tr>
      </thead>
      <tbody>
      {% for row in entry.requests %}
        <tr>
          <td class="mono url">{{ row.url }}</td>
          <td>{{ row.resource_type }}</td>
          <td>{{ row.status if row.status is not none else '—' }}</td>
          <td>{{ row.transfer_bytes|transfer_size }}</td>
          <td>{{ row.duration_ms|round|int if row.duration_ms is not none else '—' }}<span class="unit">ms</span></td>
        </tr>
      {% else %}
        <tr><td colspan="5" class="empty">
          {{ entry.degraded|join('; ') if entry.degraded else 'No requests recorded.' }}
        </td></tr>
      {% endfor %}
      </tbody>
    </table>

    {% if entry.har %}
    <p class="mono meta-line">
      HAR: {{ entry.har }}
      {% if entry.har_sha256 %}· sha256 {{ entry.har_sha256[:12] }}{% endif %}
      {% if entry.har_bytes %}· {{ entry.har_bytes|transfer_size }}{% endif %}
    </p>
    {% endif %}
  </article>
  {% else %}
  <p class="empty">No captures were recorded for this campaign.</p>
  {% endfor %}
</section>
```

Add to `report/template/style.css`:

```css
/* Appendix — one capture per block, screenshot above its request table. */
.capture { break-inside: avoid-page; margin-block-end: 2rem; }
.capture .cond { font-weight: 400; color: var(--muted); font-size: 0.85em; }
.shot { margin: 0.75rem 0; }
.shot img { max-width: 100%; height: auto; border: 1px solid var(--rule); }
.shot figcaption { font-size: 0.8rem; color: var(--muted); margin-block-start: 0.35rem; }
/* URLs are long and must wrap rather than widen the page. */
.requests .url { word-break: break-all; max-width: 24rem; }
.meta-line { font-size: 0.75rem; color: var(--muted); }
```

Check the variable names against the existing stylesheet and use whatever it already defines for muted text and rules rather than introducing new custom properties.

- [ ] **Step 4: Run the tests and regenerate the baseline**

Run: `pytest tests/unit/render_html_test.py -v`
Expected: PASS.

The baseline is now stale by exactly three entries. Do **not** hunt for a fixture
report — edit `report/skeleton.baseline.json` by hand, appending the three
entries after `"methodology"`:

```json
        "comparison",
        "methodology",
        "appendix[]",
        "appendix.screenshot",
        "appendix.requests"
```

Hand-editing is correct here precisely because the diff must be reviewable: the
baseline exists so a structural change lands as a small, deliberate diff. Then
let the test prove the hand edit matches what the template actually renders:

```bash
pytest tests/unit/skeleton_test.py::test_the_committed_baseline_matches_the_real_template -v
git diff report/skeleton.baseline.json
```

Expected: the test passes, and the diff is exactly three added lines (plus the
comma on the `"methodology"` line). If the test fails, the template's section
names or their order differ from what you wrote — fix the baseline to match the
render, never the reverse. If the diff shows anything else moving, a section
was displaced and must be understood before committing.

Run: `pytest tests/unit/skeleton_test.py -v`
Expected: PASS, including `test_the_committed_baseline_matches_the_real_template`.

- [ ] **Step 5: Commit**

```bash
git add report/render_html.py report/template/ report/skeleton.baseline.json tests/unit/render_html_test.py
git commit -m "Render the capture appendix and pin its place in the skeleton"
```

---

### Task 6: The Markdown mirror

**Files:**
- Modify: `report/render_md.py:33-38,55-57`, `report/template/report.md.j2` (append)
- Test: `tests/unit/render_md_test.py`

**Interfaces:**
- Consumes: `Report.appendix` (Task 2)
- Produces: `render_md(report, *, base_dir: Optional[Path] = None) -> str`

The mirror links screenshots by path instead of embedding them — megabytes of base64 in a Markdown file helps nobody.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/render_md_test.py`:

```python
def test_the_mirror_carries_the_appendix_section():
    md = render_md(Report.model_validate(a_report(appendix=[an_appendix_entry()])))
    assert "## Appendix" in md


def test_the_screenshot_is_linked_relative_to_the_report(tmp_path):
    shot = tmp_path / "raw" / "homepage" / "screenshot.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"")
    out = tmp_path / "reports" / "campaign"
    out.mkdir(parents=True)

    report = Report.model_validate(
        a_report(appendix=[an_appendix_entry(screenshot=str(shot))])
    )
    md = render_md(report, base_dir=out)
    assert "../../raw/homepage/screenshot.png" in md.replace("\\", "/")


def test_an_unrelatable_path_falls_back_to_absolute(tmp_path):
    # Windows: a report on C: and artifacts on D: have no relative path.
    report = Report.model_validate(
        a_report(appendix=[an_appendix_entry(screenshot=str(tmp_path / "s.png"))])
    )
    md = render_md(report, base_dir=None)
    assert str(tmp_path / "s.png").replace("\\", "/") in md.replace("\\", "/")


def test_the_request_table_is_a_markdown_table():
    md = render_md(Report.model_validate(a_report(appendix=[an_appendix_entry()])))
    assert "| Request | Type | Status | Transfer | Time |" in md


def test_a_degraded_capture_states_its_reason_in_the_mirror():
    report = Report.model_validate(a_report(appendix=[
        an_appendix_entry(requests=False, degraded=["HAR malformed: line 1"])
    ]))
    assert "HAR malformed: line 1" in render_md(report)


def test_an_empty_appendix_still_renders_the_heading():
    assert "## Appendix" in render_md(Report.model_validate(a_report(appendix=[])))
```

Copy `an_appendix_entry` and the extended `a_report(...)` helper from Task 5 into this test file (or lift both into a shared `tests/unit/conftest.py` if the file already has one).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/render_md_test.py -k appendix -v`
Expected: FAIL — `AssertionError: '## Appendix' not in ...`

- [ ] **Step 3: Write the implementation**

In `report/render_md.py`, extend `MD_SECTIONS`:

```python
MD_SECTIONS: Tuple[str, ...] = (
    "Executive summary",
    "Pages",
    "Cross-page comparison",
    "Methodology",
    "Appendix",
)
```

Add the path helper and the new parameter:

```python
def _link_path(path: Optional[str], base_dir: Optional[Path]) -> str:
    """A screenshot path as the Markdown should link it.

    Relative to the report when a relative path exists, absolute otherwise.
    On Windows a report on ``C:`` and artifacts on ``D:`` have no relative
    path at all, and ``relative_to`` raises rather than returning something
    usable — so the absolute path is the honest fallback, not a failure.
    """
    if not path:
        return ""
    target = Path(path)
    if base_dir is not None:
        try:
            import os

            return Path(os.path.relpath(target, base_dir)).as_posix()
        except ValueError:
            pass
    return target.as_posix()


def render_md(report: Report, *, base_dir: Optional[Path] = None) -> str:
    """Render the Markdown mirror.

    ``base_dir`` is where the ``report.md`` will be written, used to link
    screenshots relatively. The mirror links rather than embeds: a data URI
    that makes sense in a self-contained HTML file is megabytes of noise in a
    document meant to be read as text in a pull request.
    """
    env = _env()
    env.filters["link_path"] = lambda p: _link_path(p, base_dir)
    env.filters["transfer_size"] = transfer_size
    return env.get_template(MD_TEMPLATE).render(report=report)
```

Import `Optional` from `typing` and `transfer_size` from `report.render_html` alongside the existing `metric_label` import.

Append to `report/template/report.md.j2`:

```jinja

## Appendix

{% for entry in report.appendix %}
### {{ entry.page }} — {{ entry.device }} / {{ entry.network }}

`{{ entry.run_id }}`

{% if entry.screenshot %}
![Screenshot of {{ entry.page }} on {{ entry.device }} over {{ entry.network }}]({{ entry.screenshot|link_path }})
{% else %}
_No screenshot was retained for this capture._
{% endif %}

{% if entry.total_requests %}
Heaviest {{ entry.requests|length }} of {{ entry.total_requests }} requests · {{ entry.total_transfer_bytes|transfer_size }} transferred in total.
{% else %}
_{{ entry.degraded|join('; ') if entry.degraded else 'No requests were recorded for this capture.' }}_
{% endif %}

| Request | Type | Status | Transfer | Time |
| --- | --- | --- | --- | --- |
{% for row in entry.requests %}
| `{{ row.url }}` | {{ row.resource_type }} | {{ row.status if row.status is not none else '—' }} | {{ row.transfer_bytes|transfer_size }} | {{ row.duration_ms|round|int if row.duration_ms is not none else '—' }} |
{% endfor %}
{% if entry.har %}

HAR: `{{ entry.har }}`{% if entry.har_sha256 %} · sha256 `{{ entry.har_sha256[:12] }}`{% endif %}
{% endif %}

{% else %}
_No captures were recorded for this campaign._
{% endfor %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/render_md_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add report/render_md.py report/template/report.md.j2 tests/unit/render_md_test.py
git commit -m "Mirror the appendix in Markdown by linking, not embedding"
```

---

### Task 7: Wire it to the CLI and settings

**Files:**
- Modify: `report/__main__.py:92-115,117-141,174-200`, `requirements.txt`
- Test: `tests/unit/cli_test.py`, `tests/integration/`

**Interfaces:**
- Consumes: `report.images.build_appendix_images` (Task 3), `render_html(report, images=...)` (Task 5), `render_md(report, base_dir=...)` (Task 6), `settings.report.appendix` (Task 2)
- Produces:
  - `write_outputs(report, *, output_dir, with_pdf, images=None) -> List[Path]`
  - `python -m report --no-appendix-images`

The settings model already exists — Task 2 added it, because `_appendix` could
not run without it. This task only consumes it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cli_test.py`:

```python
def test_no_appendix_images_leaves_no_embedded_image(tmp_path, a_report_json):
    out = tmp_path / "out"
    code = report_main([
        "--input", str(a_report_json), "--output-dir", str(out),
        "--no-pdf", "--no-appendix-images",
    ])
    assert code == 0
    assert "data:image/png;base64," not in (out / "report.html").read_text(encoding="utf-8")


def test_the_appendix_section_renders_even_with_images_disabled(tmp_path, a_report_json):
    out = tmp_path / "out"
    report_main(["--input", str(a_report_json), "--output-dir", str(out),
                 "--no-pdf", "--no-appendix-images"])
    assert 'data-section="appendix"' in (out / "report.html").read_text(encoding="utf-8")


def test_a_report_whose_artifacts_were_deleted_still_renders(tmp_path, a_report_json):
    # data/raw gets cleaned; a months-old campaign must still re-render.
    out = tmp_path / "out"
    assert report_main(["--input", str(a_report_json), "--output-dir", str(out),
                        "--no-pdf"]) == 0
```

Follow the existing fixture and helper conventions in `tests/unit/cli_test.py` for `a_report_json` and however that file already invokes the report entry point.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/cli_test.py -k appendix -v`
Expected: FAIL — `unrecognized arguments: --no-appendix-images`

- [ ] **Step 3: Write the implementation**

In `report/__main__.py`, change `write_outputs`:

```python
def write_outputs(
    report: Report,
    *,
    output_dir: Path,
    with_pdf: bool,
    images: Optional[Mapping[str, Any]] = None,
) -> List[Path]:
    """Write report.html, report.md and optionally report.pdf."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    html = render_html(report, images=images)
    html_path = output_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    written.append(html_path)

    md_path = output_dir / "report.md"
    md_path.write_text(render_md(report, base_dir=output_dir), encoding="utf-8")
    written.append(md_path)

    if with_pdf:
        from report.render_pdf import chromium_page_factory, render_pdf

        pdf_path = output_dir / "report.pdf"
        pdf_path.write_bytes(render_pdf(html, page_factory=chromium_page_factory))
        written.append(pdf_path)

    return written
```

Add the flag in `_build_parser`, beside `--no-pdf`:

```python
    p.add_argument("--no-appendix-images", action="store_true",
                   help="Do not embed screenshots. A capture of an "
                        "authenticated page shows whatever was on screen, and "
                        "the PDF gets emailed.")
```

In `main`, build the images before `write_outputs`:

```python
    images = None
    if not args.no_appendix_images:
        from config.load import ConfigError, load_settings
        from report.images import build_appendix_images

        # Rendering an existing report.json must not depend on the config
        # being loadable — the report is already assembled, and the appendix
        # degrades to path-only rows exactly as it does for a missing capture.
        try:
            settings = load_settings()
        except ConfigError as exc:
            print(f"Appendix images skipped: {exc}", file=sys.stderr)
        else:
            appendix_cfg = settings.report.appendix
            images = build_appendix_images(
                report,
                root=Path(settings.storage.raw_dir).resolve(),
                width=appendix_cfg.screenshot_width_px,
                max_height=appendix_cfg.screenshot_max_height_px,
            )
```

and pass `images=images` into the `write_outputs(...)` call. Add `Any` and `Mapping` to the `typing` import. Confirm `ConfigError` is the exception `config/load.py` actually raises before importing it by that name.

In `requirements.txt`, add beside matplotlib:

```
pillow==12.3.0            # verified in venv (arrived with matplotlib); now a
                          # direct dependency — report/images.py downscales
                          # screenshots for the appendix. Pinned explicitly
                          # because this project pins what it imports.
```

- [ ] **Step 4: Run the full offline suite**

Run: `pytest -m "not e2e"`
Expected: PASS, coverage still ≥80%.

- [ ] **Step 5: Commit**

```bash
git add config/load.py config/settings.yaml report/__main__.py requirements.txt tests/
git commit -m "Wire the appendix through settings and the report CLI"
```

---

### Task 8: Documentation and roadmap

**Files:**
- Modify: `README.md:15-34,296-374,410-427`, `docs/PROJECT_SPEC.md:537`, `docs/SECURITY_PLAN.md`

**Interfaces:** none — documentation only.

The README must always state where the project is and what is missing, before any PR is opened.

- [ ] **Step 1: Update the README**

In "Where the project is", add the appendix to the working list and remove the "Screenshot / HAR appendix" row from the Missing table.

In "The report", add after the trends paragraphs:

```markdown
**The appendix carries the evidence.** Each capture gets its screenshot embedded
as a data URI — the PDF is printed via `set_content` with no origin, so a
`file://` reference would resolve to nothing — plus the heaviest requests from
its scrubbed HAR. The table is truncated to the largest transfers and always
states the true request count and total bytes beside it, because 15 rows summing
to 2 MB reads as the whole page unless the document says the page made 214
requests totalling 8 MB.

Full-page captures run to tens of thousands of pixels tall. Past
`settings.report.appendix.screenshot_max_height_px` the image is cropped from the
top and the caption says so; scaling one to fit would produce a smear a reader
cannot distinguish from a broken capture.

A missing screenshot, a cleaned `data/raw`, or a HAR the browser truncated
degrades that entry alone — the section and both its sub-blocks always render,
and `meta.degraded_appendix_entries` counts what analysis found missing. Use
`--no-appendix-images` when the captures show an authenticated session.
```

Update the roadmap table: 7b → `Done`, 7c → `**Next**`.

- [ ] **Step 2: Update the spec and security plan**

`docs/PROJECT_SPEC.md:537` — tick 7b and point at the design doc, matching the 7a entry's shape:

```markdown
- [x] **7b — PDF appendix with screenshots + HAR.** `analysis/appendix.py`
      reduces the scrubbed HAR to its heaviest requests; `report/images.py`
      embeds screenshots as deterministic, path-confined data URIs. Design:
      `docs/superpowers/specs/2026-08-04-phase-7b-appendix-design.md`.
```

`docs/SECURITY_PLAN.md` — add to the artifacts section:

```markdown
- **Appendix path confinement.** `report/images.py:embed_png` resolves every
  screenshot path and refuses anything outside `settings.storage.raw_dir`. The
  path arrives from `report.json`, a file on disk a user can edit; a renderer
  that base64s any path it is handed into a shareable document is a
  file-disclosure primitive.
- **Screenshots are page contents.** A capture of an authenticated page shows
  whatever was on screen — cart contents, an email address, an order number —
  and the PDF gets emailed. `report --no-appendix-images` renders path-only
  rows so the answer to "can I share this?" is not "re-run the campaign".
- **HAR URLs are re-redacted at render.** The stored HAR is scrubbed on the way
  in, but a HAR written before a scrubbing rule existed is still in the store,
  so `analysis/appendix.py` re-applies `store.artifacts.redact_url`.
```

- [ ] **Step 3: Verify the docs match the code**

Run: `pytest -m "not e2e"` and confirm the README's stated test count matches. Update the count in the Testing section if it moved.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/
git commit -m "Document the capture appendix"
```

---

### Task 9: End-to-end verification

**Files:**
- Modify: `tests/e2e/` (extend the existing PDF test)

- [ ] **Step 1: Write the e2e assertion**

Extend the existing e2e PDF test so a report carrying one appendix entry with a real screenshot produces a PDF whose byte length exceeds the same report rendered with `--no-appendix-images` — proof the image reached the print pipeline rather than being dropped silently by Chromium.

```python
@pytest.mark.e2e
def test_an_embedded_screenshot_reaches_the_pdf(tmp_path):
    report = a_report_with_a_real_screenshot(tmp_path)
    with_images = render_pdf(render_html(report, images=real_images(tmp_path)),
                             page_factory=chromium_page_factory)
    without = render_pdf(render_html(report), page_factory=chromium_page_factory)
    assert len(with_images) > len(without)
```

- [ ] **Step 2: Run it**

Run: `pytest -m e2e -k appendix -v`
Expected: PASS. Requires Chromium.

- [ ] **Step 3: Run a real campaign end to end**

```bash
python -m cli ingest auto --pages homepage --runs 1
python -m cli analyze
python -m cli report --skeleton-check
```
Expected: exit 0, `skeleton ok`, and `report.pdf` containing a visible screenshot and request table. Open the PDF and look at it — the point of this phase is that a human can see the capture, and no assertion proves that.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/
git commit -m "Prove the embedded screenshot survives the print pipeline"
```

---

## Self-Review

**Spec coverage:** §3 architecture → Tasks 1, 3. §3.1 layer split → Tasks 1, 3. §3.2 new module → Task 1. §4 data model → Task 2. §4.1 ordering → Tasks 1, 2. §4.2 methodology untouched → Task 2. §5 HAR reduction → Task 1. §6.1 HTML → Task 5. §6.2 screenshots + Pillow pin → Tasks 3, 7. §6.3 Markdown → Task 6. §7 skeleton → Tasks 4, 5. §8 degradation → Tasks 1, 2, 5, 6. §9 security → Tasks 3, 7, 8. §10 testing → every task. §11 consequences → Task 8. §12 configuration → Task 7.

**Type consistency:** `summarize_capture` returns `CaptureSummary` (Task 1), consumed in Task 2's `_appendix`. `embed_png` returns `Optional[EmbeddedImage]` (Task 3), consumed by `build_appendix_images` (Task 3) and the template via `render_html(images=...)` (Task 5). `collapse(sections, *, roots=GROUP_ROOTS)` (Task 4) is used only through `fingerprint`. `transfer_size` is defined in `render_html.py` (Task 5) and imported by `render_md.py` (Task 6).

**Known deviation from the spec:** design §5 says `analysis/appendix.py` "never reads a file itself". The module has two thin I/O helpers (`read_har`, `summarize_capture`) alongside the pure `reduce_har`; something has to open the file, and putting it here keeps HAR handling in one place. The spec's §2 file list is updated to say so.
