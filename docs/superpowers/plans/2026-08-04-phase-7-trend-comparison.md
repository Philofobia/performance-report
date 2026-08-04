# Phase 7A Trend Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every page carries the history of its own metrics, under the conditions it was actually measured under, with a direction that survives run-to-run noise.

**Architecture:** `analysis/trends.py` is pure — history rows plus the current campaign's runs in, series out. The store read lives in `analysis/__main__.py` and degrades to an empty history on any failure. Rendering formats what analysis decided, as always.

**Tech Stack:** Python 3.11+, pydantic, matplotlib (Agg → SVG), Jinja2, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-04-phase-7-trend-comparison-design.md` — read it before starting.

## Global Constraints

- **Analysis decides; rendering formats.** The template computes no delta and no direction.
- **The model never supplies a number** — and never sees the history at all in this phase.
- **The trend never changes a verdict.** Verdicts stay statements about current numbers.
- **No section is ever conditionally omitted.** No history renders the empty state.
- **Analysis never fails because history is unavailable.** Missing/unreadable store ⇒ every series is `new`.
- **Series keys come from the current campaign**, never from the store.
- **Determinism:** two renders of one `report.json` stay byte-identical. Series ordering is explicit, never dict-insertion order.
- **Offline tests need no browser, no network, no API key.** History is injected in tests.
- **Test file naming:** `tests/unit/<topic>_test.py`.
- **`from __future__ import annotations`** at the top of every new module; docstrings explain *why*.
- **Coverage floor:** CI enforces ≥80%.

## File Structure

| File | Responsibility |
|---|---|
| `analysis/trends.py` | `TREND_METRICS`, `TrendPoint`, `TrendSeries`, `build_series`, `load_history` |
| `analysis/reportmodel.py` | `TrendPointModel`, `TrendSeriesModel`, `PageBlock.trends` |
| `analysis/__main__.py` | reads history, passes it to `build_report` |
| `config/load.py`, `config/settings.yaml` | `TrendsConfig` |
| `report/charts.py` | `trend_chart` |
| `report/template/report.html.j2`, `report.md.j2` | the `page.trend` section |
| `report/skeleton.baseline.json` | regenerated |
| `tests/unit/trends_test.py` | series assembly and direction |

---

### Task 1: Config

**Files:**
- Modify: `config/load.py`, `config/settings.yaml`
- Test: `tests/unit/config_test.py`

**Steps:**
- [ ] `TrendsConfig(dead_band_pct: float = 5.0, ge=0; window: int = 5, ge=2)`, wired as `Settings.trends`.
- [ ] `window` must be ≥2: a window of 1 could never produce a direction, and silently rendering every series as `new` would look like missing data rather than a misconfiguration.
- [ ] Add the block to `settings.yaml` with comments matching the file's register.
- [ ] Tests: defaults when the block is absent; rejection of a negative dead band and of `window: 1`.

**Verify:** `pytest tests/unit/config_test.py`

---

### Task 2: `analysis/trends.py`

**Files:**
- Create: `analysis/trends.py`
- Test: `tests/unit/trends_test.py`

**Steps:**
- [ ] `TREND_METRICS = ("lcp_ms", "cls", "inp_ms", "tbt_ms")`, and a metric → threshold-attribute map for targets (TBT has none).
- [ ] `TrendPoint` and `TrendSeries` dataclasses, mirroring how `findings.py` returns dataclasses that `reportmodel` converts.
- [ ] `direction(previous, latest, dead_band_pct) -> str` — the §5 table, including the `previous == 0` case.
- [ ] `crossed(previous, latest, target) -> Optional[str]` — `into_fail` / `into_pass` / `None`.
- [ ] `build_series(runs, *, history, thresholds, dead_band_pct, window) -> Dict[str, List[TrendSeries]]` keyed by page name. Series keys from `runs`; history filtered to those keys; current point appended and deduped by `run_id`; truncate to the newest `window`; order by `(device, network, TREND_METRICS index)`.
- [ ] `load_history(db_path, *, project) -> List[dict]` — one `sql.metric_history` call per metric with the metric name attached to each row. Returns `[]` for a missing file, `StoreError`, `sqlite3.Error` or `OSError`.
- [ ] Tests: every row of the §9 table.

**Verify:** `pytest tests/unit/trends_test.py`

---

### Task 3: Report JSON

**Files:**
- Modify: `analysis/reportmodel.py`, `analysis/__main__.py`
- Test: `tests/unit/reportmodel_test.py`, `tests/integration/analysis_pipeline_test.py`

**Steps:**
- [ ] `TrendPointModel` and `TrendSeriesModel` with `.of()` classmethods, matching `ProjectionModel.of`.
- [ ] `PageBlock.trends: List[TrendSeriesModel] = []` — defaulted, so an existing `report.json` still validates.
- [ ] `build_report(..., trends=None)`; `_page_block` looks its page up, defaulting to `[]`.
- [ ] `run_analysis(..., history=None)`: when `history` is None, read it via `load_history`; when injected (tests), use it as given.
- [ ] Tests: trends reach the JSON; a report with no history round-trips with empty lists.

**Verify:** `pytest tests/unit/reportmodel_test.py tests/integration/analysis_pipeline_test.py`

---

### Task 4: Chart and templates

**Files:**
- Modify: `report/charts.py`, `report/template/report.html.j2`, `report/template/report.md.j2`
- Test: `tests/unit/charts_test.py`, `render_html_test.py`, `render_md_test.py`

**Steps:**
- [ ] `trend_chart(series) -> str` — pure line chart, pinned hashsalt, `metadata={"Date": None}`, explicit `figsize`, palette colours. Refuses to draw for fewer than two points and returns the empty-state marker instead.
- [ ] `data-section="page.trend"` after `page.cwv-dashboard`, always rendered, with the "no prior campaigns for this condition" empty state.
- [ ] Markdown mirror: the same section as a table; add it to `MD_SECTIONS`.
- [ ] Tests: chart structure and determinism; section always present; empty state; direction label rendered.

**Verify:** `pytest tests/unit/charts_test.py tests/unit/render_html_test.py tests/unit/render_md_test.py`

---

### Task 5: Baseline and integration

**Files:**
- Modify: `report/skeleton.baseline.json`
- Test: `tests/integration/analysis_pipeline_test.py`, `tests/integration/cli_test.py`

**Steps:**
- [ ] Regenerate the baseline with `python -m cli report --update-baseline` and confirm the diff is exactly the one added `page.trend` entry.
- [ ] Integration: seed a store with an earlier campaign, analyse a second, assert the direction reported.
- [ ] Integration: no store ⇒ every series `new`, report still complete.

**Verify:** `pytest -m "not e2e"`

---

### Task 6: Documentation

**Files:**
- Modify: `README.md`, `docs/PROJECT_SPEC.md`, `docs/TESTING_PLAN.md`

**Steps:**
- [ ] README: trend subsection under "The report"; status and roadmap updated with the Phase 7 row split so only the trend half reads as done; test count refreshed.
- [ ] PROJECT_SPEC §10 Phase 7 split the same way.
- [ ] TESTING_PLAN: rows for `analysis/trends.py` and the trend chart.

**Verify:** `pytest -m "not e2e"`
