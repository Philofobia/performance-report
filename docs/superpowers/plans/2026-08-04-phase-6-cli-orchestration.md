# Phase 6 CLI Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One entry point for the whole pipeline, and a `--skeleton-check` flag that enforces the fixed-skeleton promise against a committed baseline.

**Architecture:** `cli.py` is a dispatching façade — it consumes the command token and forwards the remaining argv verbatim to each stage's existing `main(argv) -> int`. No stage's parser moves. The drift guard lives in `report/`, next to the template it guards.

**Tech Stack:** Python 3.11+, argparse, difflib, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-04-phase-6-cli-orchestration-design.md` — read it before starting.

## Global Constraints

- **The façade computes nothing.** Routing, `--help`, and two exit codes. Any logic that appears in `cli.py` belongs in a stage instead.
- **Verbatim forwarding.** `cli.py` never redeclares a stage's flags, so it can never drop one.
- **Existing entry points keep working.** `python -m analysis`, `python -m report`, `python -m ingest.automated`, `python -m ingest.manual` are unchanged in behaviour and exit codes.
- **No section is ever conditionally omitted** (Phase 5 rule, still load-bearing): the baseline only means something because the skeleton is unconditional.
- **Offline tests need no browser, no network, no API key.**
- **Test file naming:** `tests/unit/<topic>_test.py`.
- **CLI convention:** `argparse`, `_build_parser() -> argparse.ArgumentParser`, `main(argv: Optional[List[str]] = None) -> int`.
- **`from __future__ import annotations`** at the top of every new module; docstrings explain *why*.
- **Coverage floor:** CI enforces ≥80%.

## File Structure

| File | Responsibility |
|---|---|
| `cli.py` | `python -m cli` — command table and dispatch |
| `store/listing.py` | `format_run_table` (pure) + `main(argv)` for `list-runs` |
| `report/skeleton.py` | extended: `load_baseline`, `save_baseline`, `diff_sections`, `format_drift` |
| `report/skeleton.baseline.json` | committed canonical section list |
| `report/__main__.py` | extended: `--skeleton-check`, `--update-baseline` |
| `tests/unit/cli_test.py` | routing, forwarding, exit codes |
| `tests/unit/listing_test.py` | table formatting and query wiring |
| `tests/unit/skeleton_test.py` | extended: baseline round-trip, diff, committed-baseline guard |
| `tests/integration/cli_test.py` | analyze → report → skeleton-check through the façade |

---

### Task 1: Skeleton baseline and drift diff

**Files:**
- Modify: `report/skeleton.py`
- Test: `tests/unit/skeleton_test.py`

**Steps:**
- [ ] Add `BASELINE_PATH`, `BASELINE_VERSION = 1`.
- [ ] `load_baseline(path) -> List[str]` — reads the JSON, raises `ValueError` naming the path on a malformed document or a version mismatch.
- [ ] `save_baseline(sections, path) -> None` — writes `{"version": 1, "sections": [...]}` with a trailing newline so the file is diff-friendly.
- [ ] `diff_sections(expected, actual) -> List[Tuple[str, str, int]]` — `difflib.SequenceMatcher` opcodes reduced to `("-" | "+", section, index)`; empty list means identical.
- [ ] `format_drift(diff, *, path) -> str` — the human-readable block from the spec §5.3.
- [ ] Tests: round-trip; malformed and version-mismatch errors; diff for identical, added, removed and reordered lists; `format_drift` output shape.

**Verify:** `pytest tests/unit/skeleton_test.py`

---

### Task 2: Wire the flags and commit the baseline

**Files:**
- Modify: `report/__main__.py`
- Create: `report/skeleton.baseline.json`
- Test: `tests/unit/skeleton_test.py`

**Steps:**
- [ ] Add a mutually exclusive group with `--skeleton-check` and `--update-baseline`.
- [ ] After `write_outputs`, fingerprint the rendered HTML. `--update-baseline` saves and prints the count; `--skeleton-check` compares, printing either the confirmation (exit 0) or the drift block to stderr (exit 1).
- [ ] The report is written in both cases — drift is diagnosed from the artifact.
- [ ] Generate `report/skeleton.baseline.json` from a rendered synthetic report and commit it.
- [ ] Test: the committed baseline equals `fingerprint(render_html(synthetic_report))`. This is the guard that catches a stale baseline with no campaign present.

**Verify:** `pytest tests/unit/skeleton_test.py tests/unit/render_html_test.py`

---

### Task 3: `store/listing.py`

**Files:**
- Create: `store/listing.py`
- Test: `tests/unit/listing_test.py`

**Steps:**
- [ ] `format_run_table(runs) -> str` — pure. Columns: run id, page, device, network, LCP, CLS, INP. Widths from the data, header always printed, `—` for any absent metric.
- [ ] `_build_parser()` with `--db`, `--pages`, `--device`, `--network`, `--limit` (default 20).
- [ ] `main(argv)` — default `--db` from `load_settings().storage.sqlite_path`; missing file → stderr + exit 1; empty result → `no runs stored`, exit 0; `StoreError`/`OSError` → stderr + exit 1.
- [ ] Multiple `--pages` names are queried per name and concatenated, keeping newest-first order.
- [ ] Tests: formatting with and without metrics, alignment, filters reaching `store.sql.list_runs`, limit, empty store, missing database.

**Verify:** `pytest tests/unit/listing_test.py`

---

### Task 4: `cli.py`

**Files:**
- Create: `cli.py`
- Test: `tests/unit/cli_test.py`

**Steps:**
- [ ] A `COMMANDS` table mapping name → (one-line description, lazy delegate import).
- [ ] `main(argv)` — resolve the command (and the `ingest` mode), forward the remainder verbatim, return the delegate's code.
- [ ] `--help` / no arguments print the command table and exit 0; unknown command and missing/unknown ingest mode print to stderr and exit 2.
- [ ] Delegates are imported inside the dispatch so `python -m cli list-runs` does not import Playwright or matplotlib.
- [ ] Tests: each command routes to the right delegate with the exact forwarded argv; exit codes propagate; `cli report --help` reaches the report parser; unknown command → 2.

**Verify:** `pytest tests/unit/cli_test.py`

---

### Task 5: Integration test

**Files:**
- Create: `tests/integration/cli_test.py`

**Steps:**
- [ ] Build a temporary campaign, run `cli.main(["analyze", "--no-llm", ...])` then `cli.main(["report", "--no-pdf", "--skeleton-check", ...])`, assert both return 0 and the expected files exist.
- [ ] Assert a drifted baseline makes the same invocation return 1 while still writing the report.

**Verify:** `pytest tests/integration/cli_test.py`

---

### Task 6: Documentation

**Files:**
- Modify: `README.md`, `docs/PROJECT_SPEC.md`, `report/skeleton.py` docstring

**Steps:**
- [ ] README: `python -m cli` leads "Running it"; per-stage invocations kept as longhand; new `list-runs` and `--skeleton-check` sections; Phase 6 marked Done; gap table reduced to Phase 7; test count refreshed.
- [ ] PROJECT_SPEC §10 Phase 6 boxes ticked; `src/cli.py` corrected to `cli.py`.
- [ ] `report/skeleton.py` docstring: forward reference to Phase 6 replaced with what exists.

**Verify:** `pytest -m "not e2e"`
