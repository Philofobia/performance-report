# Phase 5 — Report Rendering

**Date:** 2026-08-02
**Status:** Approved design, not yet implemented
**Covers:** PROJECT_SPEC.md §6, §6.1, §6.2 and §10 Phase 5 — `report/charts.py`,
`report/template/*`, `report/render_pdf.py`, `report/render_md.py`

---

## 1. Purpose

Phase 4 produces `report.json`: a fully-ordered document with every number already
computed. Phase 5 turns it into the deliverable — a PDF whose skeleton never changes,
plus a Markdown mirror.

The division of labour is the point. **Analysis decides; rendering formats.** The
template computes nothing, derives nothing, and re-orders nothing. If a value is not
in `report.json`, it does not appear in the report.

## 2. Scope

**In:**

- `report/palette.py` — threshold → colour, in one place
- `report/charts.py` — pure data → SVG builders
- `report/template/report.html.j2` + `style.css` — the fixed skeleton and print CSS
- `report/template/report.md.j2` — the Markdown mirror
- `report/render_html.py`, `report/render_md.py`
- `report/skeleton.py` — `fingerprint(html)`, the drift guard's engine
- `report/render_pdf.py` — Chromium print-to-PDF, Playwright injected
- `report/__main__.py` — `python -m report`
- Removing the unused `reportlab` pin from `requirements.txt`

**Out (deferred, with reason):**

- The `--skeleton-check` CLI flag and its committed baseline. Roadmap places it in
  Phase 6. The fingerprint *function* is built and tested here; wiring it to a
  user-facing flag that diffs against a stored baseline is the Phase 6 half.
- The unified `src/cli.py`. Phase 6. `python -m report` is the interim seam.
- Screenshot/HAR appendix embedding beyond listing the capture paths. Phase 7 owns
  the visual appendix.

## 3. Module boundaries

| File | Responsibility | Depends on |
|---|---|---|
| `report/palette.py` | Verdict/threshold → colour tokens | stdlib |
| `report/charts.py` | `(data) -> str` SVG builders. No file I/O, no Jinja, no Report model | matplotlib, `palette` |
| `report/render_html.py` | Jinja environment, chart wiring, `render_html(report) -> str` | jinja2, `charts` |
| `report/render_md.py` | `render_md(report) -> str` | jinja2 |
| `report/skeleton.py` | `fingerprint(html) -> List[str]` | stdlib (`html.parser`) |
| `report/render_pdf.py` | `render_pdf(html, *, browser_factory) -> bytes` | playwright (injected) |
| `report/__main__.py` | CLI | the above |

`charts.py` takes plain numbers and strings, never a `Report` — so chart tests need no
fixture document, and a chart can be exercised with three floats.

## 4. Data flow

```
data/reports/<campaign-id>/report.json
  │
  └─► Report.model_validate(...)          [reuses analysis/reportmodel.py]
        │
        ├─► charts.*(...)  -> {section: svg_string}     (pure)
        │
        ├─► render_html(report, charts) -> report.html  (self-contained)
        │     ├─► render_pdf(html)      -> report.pdf   (Chromium, e2e-marked)
        │     └─► fingerprint(html)     -> ["cover", "summary", "page[]", ...]
        │
        └─► render_md(report)           -> report.md
```

Phase 5 defines **no schema of its own**. It validates the input against the `Report`
model Phase 4 already owns, so a truncated or hand-edited `report.json` fails at the
boundary with a Pydantic error instead of rendering a plausible-looking half-document.

## 5. The skeleton guarantee (§6.2)

### 5.1 How it is enforced

Every structural block in `report.html.j2` carries a stable `data-section` attribute:

```html
<section data-section="cover">           ...
<section data-section="summary">         ...
<section data-section="page" data-page="homepage">
  <div data-section="page.header">       ...
  <div data-section="page.cwv-dashboard">...
  <div data-section="page.resources">    ...
  <div data-section="page.lcp-breakdown">...
  <div data-section="page.findings">     ...
  <div data-section="page.impacts">      ...
  <div data-section="page.recommendations">...
  <div data-section="page.projections">  ...
</section>
<section data-section="comparison">      ...
<section data-section="methodology">     ...
```

`fingerprint(html)` walks the document with `html.parser`, collects `data-section`
values in document order, and collapses the repeating page block to a single `page[]`
group. The result is a flat list of strings.

### 5.2 The test that actually matters

**A one-page campaign and a three-page campaign with different verdicts must produce
the same fingerprint.**

Byte-comparing one campaign's HTML against itself only proves the renderer is a pure
function. It would pass happily while a section vanished for *every* campaign — which
is precisely how a skeleton rots. Comparing across differing data catches the real
failure: a block that disappears when its list is empty.

### 5.3 Empty states are mandatory

No section is ever conditionally omitted. A page with no recommendations renders the
recommendations block containing an explicit empty state ("No playbook-grounded
recommendations for this page"). A run without FCP renders the LCP-breakdown block
containing "Not available — FCP was not measured".

This is the rule that makes §6.2 hold, and it is enforced by the cross-campaign
fingerprint test rather than by discipline.

## 6. Charts

Six builders in `charts.py`, each a pure function returning an SVG string:

| Builder | Renders | Source fields |
|---|---|---|
| `cwv_gauges` | LCP / CLS / INP against targets | `pages[].metrics.cwp`, `pages[].targets` |
| `resource_bars` | Heaviest resources | `pages[].resources` |
| `request_type_donut` | Bytes by resource type | `pages[].resource_type_totals` |
| `lcp_phases` | Derived LCP phase breakdown (§6.3 below) | `cwp.ttfb_ms`, `fcp_ms`, `lcp_ms` |
| `projection_bars` | Before → projected after | `pages[].projections` |
| `comparison_heat` | Page × condition key metrics | `comparison[]` |

### 6.1 Determinism controls

matplotlib is not deterministic by default. Three controls, all required:

- `matplotlib.rcParams["svg.hashsalt"]` pinned to a constant string. Without it, SVG
  element ids are randomised per process and no two renders match.
- `savefig(..., metadata={"Date": None})`. Without it every SVG embeds a timestamp.
- Explicit `figsize`, explicit DPI, and DejaVu Sans (which ships with matplotlib).
  Without these, output depends on the machine's fonts and screen DPI.

`matplotlib.use("Agg")` is set at import so no display backend is ever required.

### 6.2 Why SVG

Vector output stays sharp at print DPI, the HTML stays a single self-contained file
with no asset paths for Chromium to resolve, and — the reason that matters for this
repo — SVG is *text*, so tests assert what a chart shows: bar count, label content,
the fail-red on the failing metric. A PNG can only be asserted to exist.

### 6.3 The LCP phase breakdown, stated honestly

§6.1 asks for "TTFB, resource load, element render". The ingestion layer never
captured the LCP entry's own sub-part timings, so those exact phases are not
available.

The chart renders what the data supports:

| Phase | Value | Meaning |
|---|---|---|
| Server | `ttfb_ms` | Time to first byte |
| Render-blocking | `fcp_ms − ttfb_ms` | Blocking resources before first paint |
| LCP element | `lcp_ms − fcp_ms` | LCP element load and paint |

It carries a visible caption: *"Derived from paint milestones, not from LCP sub-part
timings."* Overstating precision in a document whose purpose is trustworthy comparison
would be worse than the coarser breakdown.

When `fcp_ms` or `ttfb_ms` is missing, or the arithmetic yields a negative phase
(possible when FCP post-dates LCP on an odd run), the block renders its empty state
rather than a misleading chart.

## 7. Visual identity

The report is a **client-facing consultancy deliverable** — handed to a stakeholder who
is not an engineer. Cover page carrying project, date, tested pages and the verdict
badge; generous typography; charts that carry the argument; findings that read as prose
rather than log output. Dense raw numbers live in the methodology appendix.

The `frontend-design` skill is invoked at implementation time to establish palette,
typography and layout (PROJECT_SPEC §12). It shapes `style.css` only — never the
skeleton, which is fixed by §5.

Print specifics: `@page { size: A4; margin: ... }`, each per-page block opening with
`break-before: page`, `print-color-adjust: exact` so verdict colours survive printing,
and no reliance on the viewport.

### 7.1 Palette

`palette.py` maps verdict → colour token, computed from thresholds rather than
hard-coded per chart: pass green, warn amber, fail red (§6.2's "fixed palettes computed
from thresholds"). Every chart and every badge draws from it, so a colour is never
decided twice.

## 8. Security

Rendering is where model-authored prose first becomes executable markup. `report.json`
contains text written by a model that read untrusted retrieved context, user problem
descriptions, and resource URLs from the page under test.

- Jinja environments are created with `autoescape=True` for the HTML template.
- Chart SVG is the **only** markup injected unescaped, via an explicit `|safe` on chart
  variables. That SVG is generated by this system, never by the model.
- Resource names reaching a chart are truncated and escaped before they become
  matplotlib text.
- A test asserts that a `report.json` whose finding title contains
  `<script>alert(1)</script>` renders escaped in the HTML and does not execute.
- `render_pdf` uses `page.set_content(html)` rather than navigating to a `file://`
  URL: no navigation, no origin, and nothing for `url_safety` to gate.
- The Markdown mirror is not HTML-escaped (it is Markdown), but it is never rendered as
  HTML by this system; if a consumer converts it, that is their escaping boundary. This
  is stated in the module docstring rather than silently assumed.

## 9. CLI

```bash
python -m report                                    # newest campaign in data/reports
python -m report --campaign storefront-9f3ab120
python -m report --input data/reports/<id>/report.json
python -m report --output-dir build/reports         # default: the input's own directory
python -m report --no-pdf                           # HTML + Markdown only, no browser
```

Writes `report.html`, `report.md` and `report.pdf`. Prints the written paths. Exit
non-zero only for: no `report.json` found, invalid JSON/schema, unwritable output, or a
PDF failure when PDF was requested.

There is deliberately no `--formats` flag. It would overlap `--no-pdf` and create an
undefined combination (`--no-pdf --formats pdf`); HTML and Markdown are cheap and always
written.

`--no-pdf` exists so the whole pipeline can be exercised without Chromium — the same
courtesy `--no-llm` provides in Phase 4.

## 10. Testing

Offline suite stays browser-free. Only PDF generation is `e2e`-marked.

**`tests/unit/palette_test.py`**
- verdict → colour mapping is total (every verdict has a colour)
- a metric at exactly its threshold classifies as pass, not warn

**`tests/unit/charts_test.py`**
- each builder returns a non-empty `<svg` string
- `cwv_gauges` colours the failing metric with the fail token and the passing one with pass
- `resource_bars` renders one bar per resource, heaviest first, labels truncated
- `request_type_donut` with a single type renders one segment, not a crash
- `lcp_phases` renders three phases with the derivation caption
- `lcp_phases` with missing FCP returns the empty-state marker
- `lcp_phases` with a negative derived phase returns the empty-state marker
- `projection_bars` with no projections returns the empty-state marker
- two renders of the same input produce identical SVG (hashsalt + metadata controls)

**`tests/unit/skeleton_test.py`**
- `fingerprint` returns sections in document order
- repeating page blocks collapse to one `page[]` group
- **a 1-page and a 3-page report produce identical fingerprints**
- a report whose pages have no recommendations still yields the full fingerprint
- a template with a section removed produces a different fingerprint (the guard works)

**`tests/unit/render_html_test.py`**
- golden HTML for a fixed two-page report
- `<script>` in a finding title is escaped
- chart SVG is present and unescaped
- empty recommendations render the empty state, not a missing block
- `analysis_mode: rule_based` is surfaced visibly on the cover

**`tests/unit/render_md_test.py`**
- the Markdown's `##` heading sequence equals `MD_SECTIONS`, a module-level constant
  mirroring the HTML fingerprint minus the chart-only blocks; the same 1-page/3-page
  invariance is asserted
- tables render for metrics and comparison
- hostile prose appears literally (Markdown, not HTML) and is documented as such

**`tests/e2e/report_pdf_test.py`** (`@pytest.mark.e2e`)
- real Chromium renders the HTML to a PDF starting with `%PDF`
- output exceeds a trivial byte floor (a blank page would not)

**`tests/integration/report_pipeline_test.py`**
- `python -m report --no-pdf` over a Phase 4 `report.json` writes HTML + MD
- invalid JSON exits non-zero with a readable message, no traceback
- rendering is a pure function: same JSON in, byte-identical HTML out

Coverage stays above the CI floor of 80%.

## 11. Dependency change

`reportlab==5.0.0` is pinned as a "PDF fallback" and imported by nothing. With Chromium
print-to-PDF confirmed as the engine it is unused dependency surface, and this project
has twice removed exactly that (ChromaDB, litellm). It is removed from
`requirements.txt` in this phase, with the removal noted in the pin file's comments so
the decision is not silently re-litigated.

## 12. Definition of done

- [ ] `python -m report` over a real Phase 4 `report.json` writes `report.html`,
      `report.md` and `report.pdf`
- [ ] The PDF opens, is paginated, and every §6 section 0–7 is present in order
- [ ] Cross-campaign fingerprint test passes: 1-page and 3-page reports share a skeleton
- [ ] Hostile prose in `report.json` renders escaped
- [ ] Two renders of one `report.json` produce byte-identical HTML
- [ ] `pytest -m "not e2e"` green with no browser; `pytest -m e2e` covers the PDF
- [ ] `reportlab` removed from `requirements.txt`
- [ ] PROJECT_SPEC §10 Phase 5 checkboxes ticked; README status, roadmap and a rendering
      section updated to state that a PDF is now produced
