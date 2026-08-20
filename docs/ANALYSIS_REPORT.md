# Project Analysis Report

**Date:** 2026-08-19 · **Scope:** full codebase review — criticalities that could break the app, plus optimization opportunities (including whether to adopt libraries such as LangChain).

**Baseline evidence:** the offline suite passes clean — `825 passed, 14 deselected in 20.37s` (`pytest -m "not e2e"`). CI additionally runs pip-audit, gitleaks (working tree + history), a `.env`-tracking guard, coverage ≥ 80%, e2e browser tests, and a live-campaign skeleton gate.

> **Status — all findings below have been actioned** (branch
> `fix/analysis-report-criticalities`). C1–C6 are fixed; C4's redirect half is
> implemented as detection with the prevention gap documented as an accepted
> limitation. Of the optimizations, the retry-helper extraction, vector-store
> caching, CI caching and Dependabot are done; structured output, Ruff/mypy,
> pre-commit and the `pyproject.toml` migration are deliberately left as
> follow-ups (see §3). LangChain remains a recommendation *against*.
> This document is kept as the review record, not as an open to-do list.

**Overall verdict:** the codebase is in unusually good health — strong module boundaries, injected seams everywhere, deliberate security controls (SSRF gate, prompt-injection defence, header-injection checks, autoescaped templates, loopback-only web UI). The criticalities below are real, but most are *wiring* gaps — controls and features that exist and are tested, yet are never invoked by the production pipeline.

---

## 1. Criticalities

Ordered by severity. Format: **Severity · Location · Risk · Fix**.

### C1 — HIGH · `normalize/schema.py:48` · A real CLS measurement can crash an entire campaign

`CwpMetrics.cls` is constrained to `ge=0, le=1` (same for `target_cls`), but **CLS is unbounded** — it is the sum of layout-shift scores over the session, and badly-behaved pages genuinely score above 1.0 (Google's own docs treat ≥ 0.25 as "poor" with no upper limit; carousels and late-injected banners can push past 1). The collector in `ingest/browser/webser.py` sums shifts with no clamp, so the moment a target page measures `cls = 1.2`:

- `make_automated_run` → `Run.model_validate` raises `ValidationError`,
- `run_campaign` propagates it, `main` returns exit 1,
- **every measurement of the whole campaign — all pages, all conditions, all runs — is discarded**, including the ones that were fine.

The manual form and CLI reject the same legitimate value. This is the exact "surface the problem instead of silently emitting a partial run" mechanism firing on valid data.

**Fix:** drop `le=1` from `cls` and `target_cls` (keep `ge=0`). Optionally warn above 1 rather than reject. One-line change plus a schema test.

### C2 — HIGH · `store/sql.py` · No pipeline stage ever writes runs into the SQLite run store

`insert_run` / `insert_runs` have **no production caller** — only tests invoke them. `ingest auto`, `ingest manual`, and the web UI all write JSON files to `data/processed/`; nothing inserts them into `data/processed/runs.sqlite`. Consequences in the documented end-to-end flow:

- `python -m cli list-runs` always prints "No run store" (or "no runs stored" once `analyze` with an API key has created the empty file as a side effect of opening the vector store).
- **Campaign-over-campaign trends never accumulate.** `trends.load_history` reads the run store, finds nothing, and every series renders as `new` forever. History cannot come from `data/processed/` either, because run JSON filenames (`page__device__network.json`) are overwritten each campaign.
- `analyze --from-store` reads a store nothing fills.

The README advertises all three as working ("SQLite run store", `list-runs`, "campaign-over-campaign trends per page and condition"). The code for every piece exists and is well-tested — the ingestion stages just never call it.

**Fix:** after a successful campaign (and after a manual-run write), open `settings.storage.sqlite_path` and `insert_runs(conn, runs)` alongside the JSON output. ~10 lines in `ingest/automated.py` (and the manual paths), plus an integration test asserting `list-runs` sees a fresh campaign.

### C3 — MEDIUM · `store/artifacts.py` · HAR scrubbing exists but the pipeline never applies it

`store_artifacts` / `scrub_har_file` — the "never store an unredacted copy" control from SECURITY_PLAN §2.6, including `extra_headers` redaction for the bot-allowlist token — are called **only by tests**. In the real flow, `run_condition` writes the raw HAR to `data/raw/…`, and `run.captures.har` points at that raw file; the scrubbed `<root>/<project>/<page>/<run_id>/` store layout is never created. So `data/raw` retains HARs whose request headers carry the configured secret token verbatim (plus any cookies), indefinitely.

Mitigations already in place keep this at MEDIUM rather than HIGH: `record_har_content="omit"` (no response bodies), `.gitignore` excludes `data/raw/` and `*.har`, and the report appendix re-redacts URLs and never renders headers. But the appendix docstring's claim that "the input is the scrubbed HAR written by `store/artifacts.py`" is currently false — the input is the raw HAR.

**Fix:** call `store_artifacts(root, run, move=True, extra_headers=<configured header names>)` at the end of each `(page × condition)` in `run_campaign`, and point `run.captures` at the stored (scrubbed) paths before the Run is emitted. This also fixes C2's sibling claim "SQLite run store *with scrubbed artifacts*".

### C4 — MEDIUM · `normalize/url_safety.py` + `ingest/browser/runner.py` · SSRF gate can be bypassed by redirects and DNS rebinding

The gate validates the URL **once, before navigation**:

- **Redirects are not re-checked.** A permitted public `https` URL that 30x-redirects to `http://192.168.1.1/…` or an internal host is followed by Chromium and measured. The non-2xx guard doesn't catch it — the final document can be a happy 200 from the internal host.
- **DNS rebinding (TOCTOU).** `validate_url(resolve=True)` resolves the hostname, then Playwright resolves it *again* at navigation; a hostname that flips its A record between the two checks reaches a blocked range.

Practical risk is limited — this is a local CLI whose targets come from the operator's own `targets.yaml`, not an internet-facing service accepting arbitrary URLs — so this is primarily a gap between the documented guarantee and the implementation.

**Fix (cheap):** register a Playwright `page.route("**/*")` handler that re-runs `is_safe_url` on every request URL and aborts unsafe ones — one helper, unit-testable with the existing `_lookup` injection hook. At minimum, document both limits in `SECURITY_PLAN` as accepted.

### C5 — LOW-MEDIUM · `ingest/automated.py:193-241` · One failed condition discards every measurement already taken

`run_campaign` accumulates all runs in memory and persists nothing until the loop finishes. A `BlockedResponseError`, an unexpected exception — or C1 firing — on the last page of a 6-page × 3-condition × 5-run campaign throws away up to 89 completed browser navigations. `main` catches the exception and exits, so nothing is written.

**Fix:** write each Run's JSON as soon as its `(page × condition)` completes (move the write into the loop), and on failure report which conditions did complete. `TargetUnreachableError`'s CI-skip semantics are unaffected.

### C6 — LOW · Assorted smaller defects

| Location | Issue |
|---|---|
| `ingest/automated.py:227,368` | `page.name` flows into filesystem paths unsanitized (artifacts dir, output filename). A name containing `/`, `\`, or `:` (invalid on Windows) escapes the root or crashes the write. `store/artifacts._safe_segment` already exists — apply it here too. |
| `analysis/__main__.py:53` | `load_runs(from_store=…)` calls `sql.connect`, which **creates** the database. A mistyped `--from-store` path leaves a stray empty `.sqlite` file and reports "No runs found" instead of "no store at that path". `store/listing.py:118-121` already guards this correctly — mirror it. |
| `ingest/automated.py:111` | `median_measurement` uses `(… or target)`, so a (theoretical) `lcp_ms == 0.0` is treated as missing. Use an explicit `is None` check. |
| `ingest/browser/runner.py:287-291` | `context.tracing.stop(path=…)` only runs on the success path. If metric collection raises mid-condition, the trace is silently lost at `context.close()`. Stop tracing in a `finally` if the trace should survive failed runs (arguably the run you *most* want a trace of). |
| `rag/embeddings.py:296` | The embedding cache's `created_at` is always `NULL` — callers never pass it. Either populate it or drop the column. |

---

## 2. Optimization opportunities

### 2.1 Should this project adopt LangChain? **No — recommendation: don't.**

An honest assessment, since it was the example asked about:

- **What it would buy:** provider-swappable LLM/embedding clients, prebuilt text splitters/retrievers, tracing integrations (LangSmith).
- **What it would cost:** a large transitive dependency tree in a repo that pins every import and dropped `litellm` over 19 CVEs at its pinned version; an abstraction layer over exactly the parts this project deliberately keeps deterministic and inspectable (exact cosine top-k for reproducible reports, a hand-built prompt with injection defence, a numbers-free output contract enforced by Pydantic). LangChain's retriever/chain abstractions would sit *between* those guarantees and the code that provides them.
- The seams LangChain would provide already exist and are thinner: `EmbeddingClient` (Protocol), `VectorStore` (Protocol), injected `transport` callables. Swapping Google for another provider is a ~50-line client, not a framework.

The same reasoning applies to LlamaIndex. If the knowledge corpus ever outgrows exact search, the already-documented escape hatch (`SqliteVectorStore` → LanceDB or `sqlite-vec` behind the same Protocol) is the right-sized move — not a framework.

### 2.2 Targeted improvements that *are* worth it

**LLM layer (`analysis/llm.py`)**
- **Use `google-genai`'s native structured output.** The SDK accepts `response_schema` (a Pydantic model) in `generate_content` config alongside `response_mime_type="application/json"` — the API then constrains decoding to the schema. That would let you delete most of `extract_json` (brace-matching) and the corrective-retry turn, keeping Pydantic validation as a belt-and-braces check. Verify against the pinned SDK version's docs before switching.
- **Consider bumping the default model.** `gemini-2.0-flash` is configurable via `settings.models.llm`, but the default is aging; newer Flash generations are better at exactly this constrained-JSON, grounded-synthesis workload at similar free-tier cost.

**Retry/backoff duplication**
- `rag/embeddings.py:242-264` and `analysis/llm.py:220-242` contain the same retry loop, verbatim. Extract one `call_with_quota_backoff(fn, *, max_retries, sleep, jitter)` helper (or adopt `tenacity`, a small single-purpose dependency, if you prefer declarative retries). One implementation, one test.

**Vector retrieval (`store/vectordb.py`)**
- `query()` re-executes `SELECT … FROM embeddings` and re-builds the numpy matrix **per page analysed**. A 10-page campaign loads the full corpus 10 times (20 with `--use-priors`). Cache `(rows, matrix)` per `(kind, sources)` on the store instance for the lifetime of one analysis run — invalidate on `add`/`delete`. Determinism is unaffected; it's the same data.
- At real scale, `sqlite-vec` (SQLite extension, brute-force → indexed) or LanceDB slot behind the existing `VectorStore` Protocol, as the module already notes.

**Campaign throughput (`ingest/automated.py`)**
- The campaign is strictly sequential: pages × conditions × runs, one navigation at a time. Parallelising *same-machine* throttled measurements is a fidelity trade-off (contexts share CPU, and CPU throttling is per-session emulation, so concurrency skews the numbers) — so don't parallelise by default. But an opt-in `--parallel N` for *unthrottled* conditions, or interleaving *pages* while keeping runs of one condition serial, could cut wall-clock time substantially for large matrices. Document the fidelity caveat if added.

**CI (`.github/workflows/ci.yml`)**
- Cache pip (`actions/setup-python` `cache: pip`) and the Playwright browser download (`~/.cache/ms-playwright` keyed on the pinned Playwright version) — Chromium is re-downloaded in **both** jobs on every push today; this is the single biggest CI-minutes win available.
- Add **Dependabot** (or Renovate) for `requirements.txt` and GitHub Actions — the repo pins everything (good) but has no automated update signal, which is how pinned sets rot.

**Tooling gaps (nothing configured today)**
- **Ruff** (lint + format) and **mypy** — the codebase is heavily type-annotated already, so mypy is cheap to adopt and would have flagged some of C6 mechanically. Add both to CI.
- **pre-commit** with ruff, mypy, and `gitleaks protect` — moves the secret scan from CI-time to commit-time, where it prevents rather than detects.
- Consolidate into a **`pyproject.toml`** (PEP 621): project metadata, pinned deps, pytest config (replacing `pytest.ini`), ruff/mypy config in one file. Optionally manage with `uv` for fast, hash-locked installs.

---

## 3. What was done, and what was left

**Landed on `fix/analysis-report-criticalities`:**

| Finding | Resolution |
|---|---|
| C1 | `le=1` dropped from `cls` and `target_cls`. The eight tests that used "CLS > 1" as a convenient invalid value now use a negative one. |
| C2 | `ingest/persist.py` — campaigns write to the run store. Verified end to end: two campaigns, six runs listed, trend history 0 → 24 rows. |
| C3 | The same sink stores captures with `store_artifacts(..., move=True)`, passing every configured header name so an allowlist token is redacted too. |
| C4 | `assert_safe_chain` re-runs the guard over Playwright's `redirected_from` chain. Detection, not prevention — documented in SECURITY_PLAN §2.2. |
| C5 | `run_campaign` gained an `on_run` sink called per completed condition; the CLI names what it kept on failure. |
| C6 | All five: `--from-store` path guard, `median_measurement` zero/absent handling, tracing stopped in `finally`, `safe_segment` on page names, cache `created_at`. |
| Opt. | `call_with_quota_backoff` shared by both Google clients; `SqliteVectorStore` caches the corpus per scope; CI caches pip + Chromium; Dependabot added. |

**Deliberately not done, and why:**

- **LangChain / LlamaIndex** — recommendation stands against, for the reasons in §2.1.
- **`response_schema` structured output** — worth doing, but it changes how the
  model is constrained and wants verification against the pinned SDK plus a live
  quota to test against. Not a change to make blind alongside twelve others.
- **Ruff, mypy, pre-commit, `pyproject.toml`** — all worth adopting, all of them
  touch every file in the repo or add a CI gate that will fail on first run.
  They belong in their own change where the diff is reviewable as tooling rather
  than buried under correctness fixes.
- **Parallel campaigns** — the fidelity trade-off is real (CPU throttling is
  per-session emulation), so this needs a measurement study, not just a flag.

*Report generated from a full source review of `cli.py`, `config/`, `ingest/`, `normalize/`, `rag/`, `analysis/`, `report/`, `store/`, `webui/`, `tests/`, and `.github/workflows/ci.yml`, cross-checked against `docs/PROJECT_SPEC.md`, the `security` skill checklist, and a green run of the offline test suite.*
