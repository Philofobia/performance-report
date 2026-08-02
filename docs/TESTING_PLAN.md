# Testing Plan — Performance RAG & Reporting System

> **Status:** Draft · **Owner:** m.parisi · **Companion skill:** `.agents/skills/component-testing/SKILL.md`
> **Framework:** `pytest` (unit/component/integration) + `playwright` (Python) for E2E.
> Jest is intentionally **not** used (JS-only; this repo is Python).

## 1. Objectives
- Prove every component works in isolation (unit), together (integration), and
  end-to-end (E2E), **offline** wherever possible via mocks.
- Guarantee determinism: identical inputs ⇒ identical normalized runs and report skeleton.
- Harden the security controls from `docs/SECURITY_PLAN.md` (URL safety, templating escapes, quota/429).
- Provide a CI gate that fails fast on regressions, coverage drops, or security findings.

## 2. Test layers & how to run
| Layer | Location | Command | Requires network? |
|---|---|---|---|
| Unit/component | `tests/unit/*` | `pytest tests/unit -v` | No (services mocked) |
| Integration | `tests/integration/*` | `pytest tests/integration -v` | No |
| E2E (browser) | `tests/e2e/*` | `pytest -m e2e -v` | Yes (demo site, Playwright Chromium) |
| Full + coverage | — | `pytest -m "not e2e" --cov=src --cov-fail-under=80` | No |

Setup: `pip install -e ".[test]"` and `python -m playwright install chromium`.

## 3. Component coverage matrix
| Component | Test file | Scenarios |
|---|---|---|
| `config/` loaders | `tests/unit/config_test.py` | valid YAML; missing/malformed; unknown device/network; empty pages; defaults |
| `normalize/schema.py` | `tests/unit/schema_test.py` | valid manual+auto payloads; bad units/ranges; optional scores; missing run_id/url |
| `ingest/manual.py` | `tests/unit/manual_test.py` | text-only/metrics/combined; CLI exit codes |
| `ingest/browser/*` | `tests/unit/browser_test.py` | device+throttle resolution; HAR/trace/screenshot; campaign median; CLI overrides; URI safety |
| `ingest/browser` (E2E) | `tests/e2e/test_browser_e2e.py` | real Playwright, 1 page × (mid-mobile, desktop), complete run JSON |
| `store/sql.py` | `tests/integration/store_test.py` | in-memory insert/query/round-trip |
| `store/artifacts.py` | `tests/integration/store_test.py` | write/read screenshots+HAR to tmp |
| `store/vectordb.py` | `tests/integration/vectordb_test.py` | in-memory SQLite add/query/delete; exact top-k; deterministic tie-breaks; dim-mismatch refused |
| `rag/*` | `tests/unit/rag_test.py` | KB load+embed; retrieval ranking; prompt build; quota/429 backoff |
| `analysis/estimator.py` | `tests/unit/estimator_test.py` | before/after delta math; range clamping |
| `analysis/findings.py` | `tests/unit/findings_test.py` | localize problem; derive impact |
| `analysis/reportmodel.py` | `tests/unit/reportmodel_test.py` | fixed-skeleton Report JSON |
| `report/charts.py` | `tests/unit/report_test.py` | fixed palette/labels; no drift |
| `report/render_*` | `tests/unit/report_test.py` | template escapes LLM content (no injection) |
| `report` (determinism) | `tests/integration/determinism_test.py` | two identical campaigns ⇒ identical JSON/HTML |
| `src/cli.py` | `tests/unit/cli_test.py` | subcommand wiring; `--skeleton-check` |
| Full flow | `tests/integration/e2e_flow_test.py` | manual→storage→RAG(mock)→analysis(mock)→report |

## 4. Fixtures (shared `conftest.py`)
- `tmp_path`-scoped dirs; in-memory SQLite (runs *and* embeddings share one db).
- `sample_problem` (valid run), `sample_metrics`, `canned_embeddings`, `canned_llm_answer`.
- Mocks: Google AI client, Playwright context/page, network/HTTP.

## 5. Edge cases enforced
Empty inputs · bad/invalid metric units · unknown device/network · missing API key ·
429 quota hits · malformed JSON · SSRF URL categories (private ranges, raw IP, userinfo) ·
malicious LLM strings in report template · missing config files.

## 6. CI gate (order, all must pass)
1. `pip-audit -r requirements.txt` (fail on high/critical)
2. `gitleaks detect --source . --redact` (fail on any secret)
3. `pytest -m "not e2e" --cov=config --cov=normalize --cov-fail-under=80`
   (target the real top-level packages for the current layout; extend the
   `--cov=` list as more packages are added, e.g. `ingest`, `store`, `rag`,
   `analysis`, `report`, `src`)
4. `pytest -m e2e` (Chromium)
5. Determinism check + `cli.py --skeleton-check`

## 7. Definition of done (tests)
- [ ] Full unit+integration suite green offline with ≥80% coverage.
- [ ] E2E browser suite green on a public demo site.
- [ ] Determinism test green (identical structure across identical inputs).
- [ ] Security paths covered: URL safety, template escaping, quota/key-missing.
- [ ] CI gate runs all five steps and fails on regressions.
