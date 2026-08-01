---
name: component-testing
description: Guides writing and running unit, component, integration, and E2E tests for every component of this Python performance RAG & reporting system, using pytest (unit/component/integration) and Playwright (Python) for E2E browser tests. Use this skill whenever the user asks to "test the components", "write tests", "add unit tests", "make tests pass", or "cover X with tests".
---

# Component Testing

Purpose-built testing guidance for the Performance RAG & Reporting system
(`docs/PROJECT_SPEC.md`). It tells an agent *exactly how* to test each module in
this repo, how to mock external services so CI never needs network/API access,
and which commands to run.

## Framework decision (the "most reliable")

- **Unit / component / integration tests → `pytest`** (+ `pytest-asyncio`,
  `pytest-mock`, `pydantic`). This is the Python standard and is fully
  deterministic/offline when external services are mocked.
- **E2E browser tests → `playwright` (Python)**, matching the project's existing
  automated-ingestion layer. Follow the `webapp-testing` skill for the
  recon-then-act Playwright workflow.
- **Jest is NOT used**: it tests JavaScript only and cannot test this Python
  codebase. Do not add it.

## Invoking tests

From the repo root (Windows PowerShell or shell):

```bash
# unit + component tests only (fast, offline)
pytest tests/unit -v

# integration tests (temp dirs, in-memory SQLite, temp Chroma)
pytest tests/integration -v

# full suite + coverage
pytest -m "not e2e" --cov=src --cov-report=term-missing

# E2E browser tests (requires: python -m playwright install chromium)
pytest -m e2e -v
```

Package setup used by the suite (create if missing):

```bash
pip install -e ".[test]"          # or
pip install pytest pytest-asyncio pytest-mock pytest-cov playwright
python -m playwright install chromium
```

## Global testing conventions

1. **Never hit external services in tests.** Mock:
   - Google AI API (`litellm` / the `google-genai` client) — return canned embeddings.
   - Playwright context/page — return canned metric dicts; do not launch a browser
     except in `e2e`-marked tests.
   - Chroma — use `chromadb.Client(ephemeral)` in a temp scope.
   - Network — monkeypatch `urllib`/`httpx`/`requests` anywhere; URLs are not fetched.
2. **Determinism:** any two runs with identical inputs must produce identical
   normalized runs and report structure. Where nondeterminism exists (LLM, median
   of N), inject a fixed RNG / mock the LLM to a canned, stable answer.
3. **Fixture layout:** a shared `tests/conftest.py` giving `tmp_path`,
   `sample_metrics`, `sample_problem` (a valid run object), and canned
   embedding/LLM mocks. Reuse across modules.
4. **Edge cases belong in tests:** empty inputs, missing/invalid units, unknown
   device/network names, API-key-missing, 429 quota hits, and malformed JSON.
5. Async test functions must be `@pytest.mark.asyncio`.

## Component-by-component recipes

Use these as the contract for what "done" looks like per module.

### `config/` loaders (`targets.yaml`, `devices.yaml`, `networks.yaml`, `settings.yaml`)
- `tests/unit/config_test.py`
- Load valid YAML fixtures → assert parsed objects/schema.
- Missing file, malformed YAML, unknown device, unknown network, empty page list → raise clean errors.
- Assert defaults (mid-mobile + desktop) resolve when the matrix is omitted.

### `normalize/schema.py` (Pydantic canonical run object)
- `tests/unit/schema_test.py`
- Valid manual and automated payloads → parse cleanly.
- Wrong units (`lcp_ms` negative, `cls` out of 0–1, non-numeric) → validation errors.
- Optional Lighthouse scores accepted and validated (0–100).
- Empty/invalid `run_id`, missing `page.url` → errors.

### `ingest/manual.py` (CLI + validation)
- `tests/unit/manual_test.py`
- Text-only, metrics-only, and combined inputs → correct normalized run.
- CLI arg parsing → exit codes and messages.

### `ingest/browser/*` (`runner.py`, `lighthouse.py`, `webser.py`, `automated.py`)
- `tests/unit/browser_test.py` — with Playwright **mocked**:
  - Device descriptor resolution + CPU/network throttle presets applied.
  - HAR/trace/screenshot capture calls and artifact paths.
  - Campaign loop: for each (page × condition) run N times, median taken, one
    normalized run emitted per combination.
  - CLI overrides `--device/--network/--runs/--pages` respected.
  - URI safety (see `SECURITY_PLAN.md`): only `https`, no private ranges.
- `tests/e2e/test_browser_e2e.py` (`@pytest.mark.e2e`) — real Playwright: one
  page × (mid-mobile, desktop) on a public demo site; assert complete run JSON.

### `store/` (`sql.py`, `artifacts.py`, `vectordb.py`)
- `tests/integration/store_test.py`
- SQLite in-memory: insert runs, query by run_id/page, round-trip integrity.
- Artifacts: write/read screenshots+HAR to `tmp_path`, stable paths.
- Chroma ephemeral: embed+add+query, delete, graceful when key missing.

### `rag/*` (`knowledge.py`, `retrieve.py`, `prompt.py`)
- `tests/unit/rag_test.py` — mock the embedding client:
  - Knowledge base playbooks load and embed.
  - Retrieval returns expected chunks for a query (assert ranking/structure).
  - Prompt builder includes retrieved context + system instructions.
- `tests/unit/quota_test.py` — 429 / missing-key → clear error + retry/backoff.

### `analysis/*` (`llm.py`, `findings.py`, `estimator.py`, `reportmodel.py`)
- `tests/unit/estimator_test.py` — **math-heavy**: before/after deltas for given
  metric deltas, e.g. LCP 6200→2200ms ⇒ expected magnitude; clamp to KB ranges.
- `tests/unit/findings_test.py` — localize problem, derive impact from metrics.
- `tests/unit/reportmodel_test.py` — building Report JSON fields, fixed skeleton.

### `report/*` (`charts.py`, templates, `render_pdf.py`, `render_md.py`)
- `tests/unit/report_test.py`
  - Charts: fixed palette/labels, correct counts, no layout drift.
  - HTML template escapes LLM content (no raw injection) — assert no unescaped
    `<script>` survives from a malicious finding string.
- `tests/integration/determinism_test.py` — two identical campaigns → the emitted
  Report JSON and HTML structure are identical (ignoring run timestamps/ids).

### `src/cli.py`
- `tests/unit/cli_test.py` — `ingest/analyze/report/list-runs` wiring, exit codes,
  `--skeleton-check` verifies structure didn't drift.
- `tests/integration/e2e_flow_test.py` — end-to-end manual → storage → RAG(mocked)
  → analysis(mocked) → report; asserts a report PDF/MD was produced.

## CI gate (recommended)

A CI job should run, in order:
1. `pip-audit`/`safety` dependency scan (see `SECURITY_PLAN.md`).
2. `pytest -m "not e2e" --cov=src --cov-fail-under=80`.
3. `pytest -m e2e` (browser) on Chromium.
4. Determinism check + `--skeleton-check`.
Fail the build if coverage drops below threshold or any security scan flags a
high/critical advisory.

