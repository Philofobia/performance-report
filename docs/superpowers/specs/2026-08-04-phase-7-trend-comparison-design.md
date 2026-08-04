# Phase 7A — Trend Comparison

**Date:** 2026-08-04
**Status:** Approved design
**Covers:** PROJECT_SPEC.md §10 Phase 7, first bullet (the trend half) —
`analysis/trends.py`, `analysis/reportmodel.py`, `report/charts.py`,
`report/template/*`

---

## 1. Purpose

Every report stands alone. A campaign says the homepage LCP is 4820 ms; it
cannot say that last month it was 6200 ms, or that the fix everyone shipped in
between actually worked. `store.sql.metric_history` was written in Phase 3 and
documented as "the trend-over-time input", and nothing has consumed it since.

Phase 7A closes that: each page carries the history of its own metrics, under
the conditions it was actually measured under, with a direction the reader can
trust.

## 2. Scope

**In:**

- `analysis/trends.py` — series assembly and direction, pure
- `analysis/reportmodel.py` — `TrendPointModel`, `TrendSeriesModel`,
  `PageBlock.trends`
- `analysis/__main__.py` — history load with graceful degradation
- `config/load.py` + `config/settings.yaml` — `trends.dead_band_pct`,
  `trends.window`
- `report/charts.py` — `trend_chart`
- `report/template/report.html.j2` + `report.md.j2` — the `page.trend` section
- `report/skeleton.baseline.json` — regenerated, one added entry

**Out (deferred, with reason):**

- Screenshot/HAR appendix, web UI, CI report regeneration. Separate Phase 7
  sub-projects; each gets its own spec.
- Trends in the LLM prompt. Feeding history to the model invites it to narrate
  numbers, and the analysis contract is that it never supplies one. Revisit if
  the summary proves blind to regressions.
- Trend as an input to any verdict. See §6.

## 3. What a trend is computed over

**One series per `(page, device, network, metric)`.** A mid-mobile/slow-4g LCP
is only ever compared against other mid-mobile/slow-4g LCPs. Mixing conditions
would manufacture regressions out of nothing: a campaign that added a desktop
condition would show every page improving.

**The series keys come from the current campaign, not from the store.** A page
is reported with the conditions it was just measured under; a condition dropped
from `targets.yaml` three campaigns ago does not reappear as a trend. History
only supplies earlier points for series that exist now.

**Metrics trended:** LCP, CLS, INP, TBT — the four the comparison table already
carries. All four are lower-is-better, so direction needs no per-metric
polarity table. Order in the report follows that list, not alphabetical.

## 4. Data flow

```
store (all prior campaigns) ──┐
                              ├─► trends.build_series ─► PageBlock.trends ─► page.trend
current campaign's runs ──────┘         (pure)
```

History is read from the SQLite store at `settings.storage.sqlite_path`,
**regardless of how the current campaign's runs were loaded**. `analyze
--input-dir` is the default path and never touches the store, so without this
the common case would have no history at all.

**The current campaign's own point is appended, deduped by `run_id`**, so the
newest point is always the campaign being reported on. Under `--from-store` the
current runs are already in the history rows; the dedupe is what stops them
appearing twice.

Series are truncated to the newest `trends.window` points (default 5), after
the current point is appended.

## 5. Direction

`delta_pct` compares the newest point against the one before it:

```
delta_pct = (latest - previous) / previous * 100
```

| Condition | `direction` |
|---|---|
| `abs(delta_pct) < dead_band_pct` | `flat` |
| `delta_pct > 0` | `regressed` |
| `delta_pct < 0` | `improved` |
| only one point in the series | `new` (and `delta_pct` is null) |

The dead band exists because emulated throttling has real run-to-run variance:
without it a 3% wobble reads as a regression every campaign, and the section
becomes noise the reader learns to skip. It is `settings.trends.dead_band_pct`
(default 5.0) so a noisy target can be tuned without a code change.

`previous == 0` yields `direction: "flat"` and a null `delta_pct` rather than a
division error. A zero CLS that stays zero has not regressed.

**Target crossing is reported separately from direction.** Each series carries
the metric's configured target where one exists, and `crossed` is `into_fail`
when the latest point is above a target the previous point met, `into_pass` for
the reverse, and null otherwise. Direction answers "which way is it moving";
crossing answers "did it break the budget". A metric can improve and still fail,
and the report should be able to say both. TBT has no configured threshold, so
its `target` and `crossed` are null.

## 6. The trend never changes a verdict

Page and campaign verdicts remain statements about current numbers. A page that
passes every threshold but got 6% slower reports `regressed` in its trend and
still passes.

Folding trend into verdict would turn a green page red without any threshold
being crossed, and would make the verdict depend on which machine ran the
earlier campaign. The trend is reported alongside the verdict, never inside it.

## 7. Rendering

A `data-section="page.trend"` block inside the existing repeating per-page
block, immediately after `page.cwv-dashboard`. It collapses into the `page[]`
group, so the skeleton stays page-count independent.

`report/charts.py` gains `trend_chart(series) -> str`: a pure line chart under
the same determinism rules as every existing builder — pinned `svg.hashsalt`,
`metadata={"Date": None}`, explicit `figsize`. The failing colour comes from
`report/palette.py`, as everywhere else.

The Markdown mirror gets the same section, as a table.

The baseline is regenerated with `--update-baseline`, landing as a one-line
diff in `report/skeleton.baseline.json`. That is exactly the review mechanism
Phase 6 built, used for the first time.

## 8. Failure modes

Missing store file, unreadable store, a `StoreError`, or a series with a single
point all produce the same thing: the section renders with its explicit empty
state, "no prior campaigns for this condition".

Analysis never fails because history is unavailable — the same rule that
governs LLM degradation. The first campaign a user ever runs must produce a
complete report, and it will: every series is `new`.

No section is ever conditionally omitted; a page with no history renders the
block with the empty state, exactly as a page with no recommendations does.

## 9. Testing

| File | Covers |
|---|---|
| `tests/unit/trends_test.py` | Dead band at both edges; delta sign and magnitude; `previous == 0`; grouping by condition; series keys from the current campaign only; window truncation after the current point is appended; dedupe by run id; single-point series is `new`; target crossing both directions; deterministic ordering |
| `tests/unit/charts_test.py` (extended) | Trend chart structure, empty state, determinism |
| `tests/unit/render_html_test.py`, `render_md_test.py` (extended) | Section always present; empty state; regressed styling |
| `tests/unit/config_test.py` (extended) | `trends` defaults and validation |
| `tests/integration/analysis_pipeline_test.py` (extended) | A second campaign over a seeded store reports a direction; no store degrades to `new` |

The offline suite stays browser-free, network-free and key-free. History is
injected into `run_analysis` in tests rather than read from disk, matching how
the LLM and embedding clients are already injected.

## 10. Documentation

README gains a trend subsection under "The report" and an updated status; the
Phase 7 roadmap row is split so the trend half can be marked done while the
appendix, web UI and CI halves stay planned. PROJECT_SPEC §10 Phase 7 gets the
same split. TESTING_PLAN gains the two new rows.
