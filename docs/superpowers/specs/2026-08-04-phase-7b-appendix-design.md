# Phase 7B — Screenshot / HAR Appendix

**Date:** 2026-08-04
**Status:** Approved design
**Covers:** PROJECT_SPEC.md §10 Phase 7, second bullet — `analysis/appendix.py`,
`analysis/reportmodel.py`, `report/images.py`, `report/skeleton.py`,
`report/template/*`

---

## 1. Purpose

The report tells you the homepage is slow and which playbook says why. It does
not show you the page, and it does not show you the requests that made it slow.
Both were captured — `store/artifacts.py` has been writing scrubbed HARs and
screenshots to a run-scoped directory since Phase 3 — and the report has been
listing their *file paths* ever since.

A path in a PDF is not evidence. The person reading the report is not the person
who ran the campaign, is not on that machine, and cannot open
`data/raw/storefront/homepage/homepage-mid-mobile-3f2a/capture.har`.

Phase 7B makes the captures part of the document: the screenshot embedded, and
the HAR reduced to the table that answers "what was heavy".

## 2. Scope

**In:**

- `analysis/appendix.py` — HAR reduction to request rows (pure), plus two thin
  file-reading helpers
- `analysis/reportmodel.py` — `RequestRow`, `AppendixEntry`, `Report.appendix`,
  `ReportMeta.degraded_appendix_entries`
- `report/images.py` — PNG load, downscale, base64 data URI
- `report/render_html.py` — appendix data URIs alongside chart SVG
- `report/render_md.py` + `report/template/*` — the `appendix` section
- `report/skeleton.py` — `collapse()` generalized to N group roots
- `report/skeleton.baseline.json` — regenerated, four added entries
- `report/__main__.py` + `cli.py` — `--no-appendix-images`
- `config/load.py` + `config/settings.yaml` — `report.appendix.*`
- `requirements.txt` — explicit `pillow` pin (already present transitively)

**Out (deferred, with reason):**

- HAR waterfall chart. A 200-request waterfall is illegible at A4 width and
  needs truncation rules of its own; the top-N table answers the same question
  without them.
- Trace embedding. A Playwright trace is only useful inside the Playwright trace
  viewer. It stays a path in `methodology.captures`.
- Web UI (7C), CI report regeneration (7D). Separate sub-projects.

## 3. Architecture

```
data/raw/<project>/<page>/<run_id>/{screenshot.png, capture.har}
        │
        ├─ analysis/appendix.py ──► parse scrubbed HAR → top-N RequestRow[]
        │                            → report.json  `appendix: []`
        │
        └─ report/images.py ─────► read PNG, downscale, base64 → data: URI
                                     at render time; never in report.json
```

### 3.1 Why the work splits across two layers

The project rule is that analysis is the last stage that computes anything and
the report layer computes nothing. HAR parsing is computation: it decides which
requests matter and what the totals are. It belongs in analysis, and its output
belongs in `report.json` where it is reviewable and diffable.

Turning a path into pixels is not computation. It is the same act as
`report/charts.py` turning numbers into SVG — the report layer's actual job.

The rejected alternative was base64 in `report.json`. It gives the purest
boundary (the report layer would do no file I/O at all) and costs the thing
`report.json` exists for: a text document a human can read and `git diff` can
compare. Megabytes of base64 destroy that.

### 3.2 Why `analysis/appendix.py` is a new module

`analysis/reportmodel.py` is 440 lines and already owns assembly for every other
section. The HAR reduction is self-contained, pure, and has its own error
surface; growing `reportmodel.py` by another 150 lines would make the file worse
at the one thing it is for. `reportmodel.build_report` calls into `appendix.py`
at the point where it already walks captures.

## 4. Data model

Additive. `SCHEMA_VERSION` bumps by one; every existing field keeps its shape,
so a reader written against the previous version still parses what it knows.

```python
class RequestRow(BaseModel):
    url: str                       # re-passed through store.artifacts.redact_url
    resource_type: str             # img/script/stylesheet/font/document/media/other
    status: Optional[int] = None
    transfer_bytes: int = 0
    duration_ms: Optional[float] = None


class AppendixEntry(BaseModel):
    page: str
    run_id: str
    device: str
    network: str
    screenshot: Optional[str] = None       # path — never bytes
    har: Optional[str] = None
    har_sha256: Optional[str] = None
    har_bytes: Optional[int] = None
    requests: List[RequestRow] = []        # the top N
    total_requests: int = 0                # the true count, so N stays honest
    total_transfer_bytes: int = 0
    degraded: List[str] = []
```

`Report.appendix: List[AppendixEntry]` and
`ReportMeta.degraded_appendix_entries: int`.

**`total_requests` alongside `requests[]`** because a table of 15 rows summing to
2.1 MB, with no statement that the page made 214 requests totalling 8 MB, is a
misleading document. The count is what stops the top-N from reading as the whole.

**`har_sha256` and `har_bytes`** make the referenced file identifiable. A report
that cites a HAR by path cites something mutable; a digest lets a reader confirm
the file they opened is the file the report was built from.

### 4.1 Ordering

Entries sort by `(page, run_id)` — the key `methodology.captures` already uses,
so the two lists read in the same order.

Rows sort by `(-transfer_bytes, url)`. The URL tie-break matters: two requests
with identical byte counts are common (empty 204s, identically-sized sprites),
and without it their order comes from dict iteration and the report stops being
reproducible.

### 4.2 `methodology.captures` is left alone

`AppendixEntry` carries the screenshot and HAR paths that `CaptureRow` already
carries, so the two duplicate two strings per capture. That is deliberate:
`methodology.captures` is the trace-inclusive manifest of what exists on disk,
existing tests bind to it, and removing it would be a breaking schema change to
a section that works. Two duplicated strings are cheaper than that.

## 5. HAR reduction

Input is the **scrubbed** HAR from the store. The reduction itself — `reduce_har`
and everything it calls — is pure functions over an already-parsed dict, so the
tests that matter need no fixtures on disk. File access is confined to two thin
helpers (`read_har`, `summarize_capture`), and neither raises: something has to
open the file, and keeping that here means HAR handling lives in one module
rather than being split across two.

- **Transfer size** from `entry.response._transferSize` when present, falling
  back to `response.bodySize + response.headersSize`, clamped at zero. A HAR
  from a cache hit reports `-1`; a negative byte count in a size table is worse
  than a zero.
- **Resource type** from Playwright's `_resourceType` when present, else derived
  from `response.content.mimeType`, else from the URL extension, else `other`.
  Derivation is a pure lookup so the same HAR always classifies the same way.
- **Duration** from `entry.time`, omitted when absent rather than zeroed —
  the same rule the run listing already applies to missing metrics.
- **URL** re-passed through `store.artifacts.redact_url`. The stored HAR is
  already scrubbed, but a HAR written before a scrubbing rule existed is still
  in the store, and this layer is where it becomes a rendered document.

`top_requests` (default 15) comes from settings.

## 6. Rendering

### 6.1 HTML

A new section after `methodology`:

```
<section data-section="appendix">          ← wrapper, singular
  <article data-section="capture">         ← repeats per entry
    <figure data-section="capture.screenshot">
    <table  data-section="capture.requests">
```

`report/render_html.py:build_charts` gains an `appendix` key alongside `pages`
and `comparison`, so the data URIs travel the path the chart SVGs already
travel. The data URI is the only new `|safe` value in the template, and it is
generated from base64 by our own code — never from model prose.

### 6.2 Screenshots

`report/images.py:embed_png(path, *, width, max_height, root) -> str | None`.

- Downscale to `screenshot_width_px` (default 720).
- Cap height at `screenshot_max_height_px` (default 1600), cropping from the
  top, with a **visible caption** stating the crop. A full-page mobile capture
  is routinely 15,000px tall; scaling it to fit would render a 3mm smear, and a
  reader cannot tell a smear from a broken capture. Saying "top 1600px of a
  full-page capture" is the honest version, and it is the same reflex as the LCP
  breakdown carrying a caption about where its phases come from.
- Re-encode PNG with a pinned resampling filter and no `pnginfo`, so identical
  source bytes always produce an identical data URI. matplotlib's randomised
  element ids taught this lesson already: "same data, same report" is false the
  moment any encoder stamps anything.

**Pillow** is already installed as a matplotlib dependency (12.3.0 in the venv).
It gains an explicit pin in `requirements.txt`, because this project pins direct
dependencies deliberately and a transitive dependency with a first-party caller
is a direct dependency.

### 6.3 Markdown

The mirror cannot carry megabytes of base64. It links the screenshot by path
relative to `report.md`, falling back to an absolute path when the report and
the artifact sit on different Windows drives and no relative path exists. The
request table renders as a markdown table.

Same section sequence as the HTML, different asset strategy — which is what a
mirror is.

## 7. Skeleton

`collapse()` is hard-wired to one repeating root. It gains a `roots` parameter
defaulting to `("page", "capture")`, and its single `emitted_page_block` bool
becomes a per-root set.

The repeating entry is named `capture`, not `appendix`, because the wrapper
section is itself tagged `appendix` and the fingerprint is a *flat* token
stream — a wrapper sharing its child-block's name opens an empty group, and the
real entries are then discarded as already-emitted. The children would vanish
from the fingerprint entirely, which is the exact blindness this section rejects
two paragraphs down.

The fingerprint becomes:

```
… comparison, methodology, appendix, capture[], capture.screenshot, capture.requests
```

`BASELINE_VERSION` stays `1`. A report containing no appendix section produces a
byte-identical fingerprint under the generalized algorithm, so this is not an
algorithm change that could masquerade as drift — the baseline diff is exactly
three added lines.

The rejected alternative was tagging only the wrapper and leaving per-capture
entries untagged. It needs no change to `skeleton.py`, and it means the
screenshot figure or the request table could vanish from every report without
`--skeleton-check` noticing. That is precisely the rot the module exists to
catch, and buying a smaller diff with it is the wrong trade.

## 8. Degradation

Every entry degrades independently. The section and both sub-blocks always
render — no section is ever conditionally omitted, and neither is a sub-block.

| Condition | Where detected | Result |
| --------- | -------------- | ------ |
| No screenshot path on the run | analysis | `degraded: ["screenshot not retained"]`, figure renders its empty state |
| Path set, file gone | analysis | `degraded: ["screenshot file missing"]`, same |
| File present, will not decode | report | figure renders "capture unreadable"; `report.json` unchanged |
| No HAR path | analysis | `degraded: ["HAR not retained"]`, table renders its empty state |
| HAR malformed or truncated | analysis | `degraded: ["HAR malformed: <detail>"]`, same |

**Detection splits by layer on purpose.** Analysis stats the files and parses the
HAR, so those facts land in `report.json`. A PNG that exists but will not decode
is only discoverable when something decodes it, which is render time — and the
report layer must not reach back and edit `report.json` to record it. Rendering
the placeholder is the whole response.

`meta.degraded_appendix_entries` therefore counts analysis-time degradation, and
the spec says so rather than implying it covers everything.

Analysis never fails over an unavailable artifact. A campaign from three months
ago whose `data/raw` was cleaned still re-analyses and still produces a complete
report — the same rule that already governs unavailable trend history.

## 9. Security

Documented additions to SECURITY_PLAN.md:

- **Path confinement.** `embed_png` refuses any path resolving outside the
  artifacts root, which is `settings.storage.raw_dir` resolved to an absolute
  path by `report/__main__.py` and passed in — the renderer never infers it from
  the paths it is checking. The path arrives from a JSON file on disk that a
  user can hand-edit; treating it as trusted is how a report renderer becomes a
  file-disclosure primitive.
- **URL redaction is re-applied**, not assumed (§5).
- **`--no-appendix-images`** renders path-only rows. A screenshot of an
  authenticated page contains whatever was on screen — cart contents, an email
  address, an order number — and the PDF gets emailed. The flag exists so the
  answer to "can I share this?" is not "re-run the whole campaign".
- **Data URIs are generated, never accepted.** The base64 alphabet is asserted
  before the value is marked `|safe`.

## 10. Testing

**Unit — `analysis/appendix.py`:** top-N selection and ordering, the URL
tie-break, transfer-size fallback chain, negative `_transferSize` clamped,
resource-type derivation for each branch, `total_requests` vs `len(requests)`,
malformed HAR, truncated HAR, HAR with no `log.entries`, empty entries list,
URL re-redaction.

**Unit — `report/images.py`:** downscale dimensions, height crop applied and
captioned, byte-for-byte identical output across two calls, unreadable file
returns `None`, path outside the artifacts root returns `None`, data URI
alphabet.

**Unit — `report/skeleton.py`:** generalized `collapse()` against the existing
page fixtures produces unchanged output; `appendix` children fold correctly;
a root with no children is still emitted.

**Integration:** **one capture vs six captures → identical fingerprint**, which
is the cross-campaign test extended to the new repeating block — the same
argument as one page vs three pages · missing screenshot still renders the
figure · malformed HAR still renders the table · committed baseline matches the
synthetic render · `--no-appendix-images` produces no `data:` URI.

**E2E:** real Chromium PDF still produced, with images embedded.

## 11. Consequences

PDF size grows materially — six 720px captures is roughly 1–3 MB against the
current tens of KB. That is the feature working, not a regression, but it is
worth stating: the report becomes an artifact you attach rather than paste.

## 11a. Known limitations

Two things the final review surfaced that ship as-is, recorded so nobody has to
rediscover them.

**A request with no recorded size renders "0 B", not "—".** §4 types
`RequestRow.transfer_bytes` as a non-Optional `int`, and `entry_transfer_bytes`
returns `0` when `_transferSize`, `bodySize` and `headersSize` are all absent —
indistinguishable from a confirmed zero. That violates the project's rule that a
value which does not exist prints `—`, never `0`, and the rule is stated in
`transfer_size`'s own docstring. The branch honours it everywhere the value can
actually be `None`; the appendix table is the one place the type forbids `None`
so the `—` branch is unreachable. Cache hits legitimately transfer zero bytes and
the size ranking is unaffected, so this is cosmetic rather than misleading — but
fixing it properly means making the field Optional, which is a schema change.

**Markdown screenshot paths are not confined to the artifacts root.**
`report/images.py` refuses to *open* any path outside `settings.storage.raw_dir`,
because embedding reads the file. `render_md.link_path` only *writes* the path
into a link and never reads it, so there is nothing to disclose and no check is
applied. The asymmetry is deliberate, not an oversight.

## 12. Configuration

```yaml
report:
  output_dir: data/reports
  appendix:
    # How many requests the per-capture table shows, largest transfer first.
    top_requests: 15
    # Screenshots are downscaled before embedding; a full-page mobile capture
    # is otherwise several MB of base64 per entry.
    screenshot_width_px: 720
    # Full-page captures run to tens of thousands of pixels tall. Beyond this
    # the image is cropped from the top and the crop is stated in the caption.
    screenshot_max_height_px: 1600
```

```bash
python -m cli report --no-appendix-images   # path-only rows, no embedded images
```
