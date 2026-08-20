# A report a project manager can read — design

**Date:** 2026-08-20
**Status:** approved, ready for implementation

## Problem

The pipeline now produces a genuinely grounded report, and it is still the wrong
document for most of its readers. Reviewing the live Oakley run:

- **Nothing is prioritised across pages.** All three executive actions came from
  `homepage` because it sorts first alphabetically, while the PDP measured a
  Total Blocking Time of 8636 ms — four times worse — and 29,502 DOM nodes.
- **No number has any context.** `TBT 2041ms` appears with no target beside it,
  though `PageBlock.targets` is already in the Report JSON and simply never
  reaches the page.
- **No term is ever explained.** TBT, INP, render-blocking stylesheets, style
  recalculation — all assumed knowledge.
- **"What it costs" is boilerplate.** *"Users experience delays in page
  responsiveness"* tells a reader nothing the test was needed to discover.
- **Machine output leaks into prose.** The trend table prints
  `2438.5999999940395` and `0.015911182251991944`, twenty-four rows of them, every
  one saying `new / —` on a first campaign. Projections render as
  `tbt_ms 2041 → 1633; tbt_ms 1633 → 1372`, the stacked-discount pairs showing
  through.
- **The document is mostly pictures.** Forty PDF pages for a three-page campaign:
  six full-page screenshots at `max-width: 100%`, each followed by fifteen rows
  of raw URLs.

**Goal:** one report that a project manager can read start to finish and act on,
without taking anything away from the developer who has to do the work.

## Approach

A **reader layer** over data the pipeline already produces. Measurement, the
estimator, the store and the RAG retrieval are untouched — this is a change to
what gets said and in what order, not to what gets measured.

Three new pieces, then a restructuring of the renderers around them:

1. a committed **glossary** giving every metric a plain-language gloss and a
   target,
2. a deterministic **cross-page ranker** producing one ordered action plan,
3. two plain-language fields added to the model's **output contract**.

## Design

### 1. Glossary — `data/knowledge/glossary.yaml`, `report/glossary.py`

One entry per metric:

```yaml
tbt_ms:
  label: Total blocking time
  unit: ms
  round: integer
  target_key: null            # no threshold configured; context comes from the gloss
  plain: >
    How long the page ignores taps and clicks after it first appears on screen.
lcp_ms:
  label: Largest contentful paint
  unit: ms
  round: integer
  target_key: lcp_good_ms
  plain: >
    How long until the main thing on the page — usually the big image or
    headline — has finished drawing.
```

`target_key` names a field on `settings.thresholds`, so targets stay configured
in one place. `report/glossary.py` loads and validates the file, and exposes:

- `gloss(metric) -> str` — the plain sentence,
- `label(metric) -> str` — the display name,
- `format_value(metric, value) -> str` — rounding and unit, so `2438.599…`
  renders `2439 ms` and CLS renders `0.02` wherever either appears,
- `context(metric, value, target) -> str` — `"10× over"`, `"within target"`,
  `"1.2× over"`.

The glossary is data, committed, and reviewed as a diff. The model never writes
these sentences, which is what keeps wording identical run to run — the
comparability the whole project exists for.

A metric with no glossary entry renders exactly as it does today rather than
raising: an unglossed metric is a documentation gap, not a broken report.

### 2. Cross-page ranking — `analysis/priority.py`, `Report.action_plan`

```python
class PlannedAction(BaseModel):
    rank: int
    page: str
    title: str
    why_it_matters: str          # plain, from the model
    effort: str
    metric: Optional[str]        # the metric it improves
    projected: Optional[str]     # "2041 ms → 1633 ms", already formatted
    playbook_source: str
```

Scoring, entirely rule-based over projections the estimator already computes:

```
severity_weight = 2.0 if the page's symptom for that metric is "fail"
                  1.0 if "warn"
                  0.5 otherwise
gap    = max(0, value - target)          # 0 when no target is configured
gain   = projection.before - projection.after_high     # conservative bound
score  = severity_weight * min(gain, gap) if gap else severity_weight * gain
```

Ties break on page name, then title, so the order is deterministic — a
requirement, not a nicety: two runs of the same campaign must produce identical
documents (§6.2).

`summary.top_actions` becomes the plan's first three entries. Today it is
whichever page sorted first, which is how a homepage recommendation outranked an
8636 ms blocking time.

A campaign with no projections at all (rule-based mode, no playbook magnitudes)
produces an empty plan, and the plan section says so in one line rather than
disappearing — a vanishing section is exactly the drift `skeleton.py` exists to
catch.

### 3. Output contract — two new fields

```python
class LlmFinding(BaseModel):
    ...
    consequence: str = Field(default="", max_length=500)

class LlmRecommendation(BaseModel):
    ...
    why_it_matters: str = Field(default="", max_length=500)
```

Both default to empty, so a model that omits them degrades to today's output
rather than failing the contract. The prompt gains one instruction: the reader is
a project manager with no performance background; state the consequence in plain
language, in terms of what a visitor to *this page* experiences; do not restate
metric values, and do not include numbers — the existing rule that magnitudes
come only from playbook metadata is unchanged, as is the citation guard.

These replace the `impacts` boilerplate as the page's "what it costs" content.
`impacts` stays in the Report JSON and in the technical detail block: it is
already rendered, already tested, and removing it would be a second, unrelated
change.

### 4. Numbers in context — the at-a-glance table

One table per page, above the findings:

| Metric | Measured | Target | Verdict | What it means |
| --- | --- | --- | --- | --- |
| Total blocking time | 2041 ms | 200 ms | 10× over | The page ignores taps for about 2 seconds after it appears. |
| Largest contentful paint | 2297 ms | 2500 ms | within target | The main image finishes drawing this long after the page starts loading. |

Built from `PageBlock.metrics`, `PageBlock.targets` and the glossary — all three
already exist; only the last is new. Values run through `format_value`, so the
raw-float problem is fixed at the point every renderer shares.

### 5. Skeleton — a deliberate baseline rewrite

```
cover
summary
plan                          (new)
page[]
  page.header
  page.at-a-glance            (new)
  page.findings
  page.recommendations
  page.detail                 (new wrapper)
    page.cwv-dashboard
    page.resources
    page.lcp-breakdown
    page.trend
    page.projections
comparison
methodology
appendix
capture[]
  capture.screenshot
  capture.requests
```

Plain language first, evidence after, under a heading that says it is technical
detail. `report/skeleton.baseline.json` is regenerated by
`report --update-baseline` in its own commit, so the change lands as a reviewable
diff — the mechanism by which drift stays visible rather than merely detectable.

The trend block stays present on a first campaign and renders one line — *"First
campaign — no history to compare yet."* — instead of twenty-four rows of
`new / —`. Present-but-empty keeps the fingerprint stable; the noise goes.

Projections render as a single conservative range per metric
(`2041 ms → 1633–1372 ms`), not as the stacked-discount pairs.

### 6. Images

`config/settings.yaml`:

```yaml
report:
  appendix:
    screenshot_width_px: 480          # was 720
    screenshot_max_height_px: 400     # was 1600
```

The crop is already stated in the caption by existing code, and the
full-resolution capture already lives under `data/raw/…` and is already
referenced by path in the appendix. Print CSS gains `max-height` on
`.shot img` so a capture cannot own a page in the PDF regardless of its
aspect ratio. Expected: ~40 pages down to roughly 12.

### 7. Backwards compatibility

Every new Report JSON field defaults — `action_plan` to `[]`, `consequence` and
`why_it_matters` to `""` — so a `report.json` written before this change still
validates and still renders, exactly as `trends` was handled in Phase 7.

### 8. Testing

- `tests/unit/glossary_test.py` — every metric the report can render has an
  entry; `format_value` rounding for ms, CLS, percentages, bytes; `context`
  wording at, under and over target; an unglossed metric falls back rather than
  raising.
- `tests/unit/priority_test.py` — the 8636 ms PDP action outranks the 2041 ms
  homepage action; ordering is stable across two identical runs; ties break by
  page then title; no projections produces an empty plan, not a crash.
- `tests/unit/reportmodel_test.py` — `top_actions` comes from the plan; a
  pre-change `report.json` still validates.
- `tests/unit/skeleton_test.py` — the new section list is the baseline, and a
  report rendered against the old baseline fails the drift check.
- `tests/unit/render_*_test.py` — the at-a-glance table carries target and
  verdict columns; the first-campaign trend placeholder renders; no raw float
  with more than two decimals appears in any rendered output.
- `tests/integration/report_pipeline_test.py` — end to end: plan ordered by
  score, findings carrying consequences, PDF page count materially lower than
  the screenshot-per-page shape.

## Out of scope

- Changing what is measured, or the estimator's arithmetic. Every number in the
  report still comes from the same place it does today.
- Removing `impacts` from the Report JSON.
- A separate executive-brief document. One report, layered, serves both readers;
  two documents would need keeping in sync.
- Translating the report into other languages.
