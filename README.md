# Performance RAG & Reporting System

Capture web performance data, reason over it with a retrieval-grounded LLM, and emit a
**report whose skeleton never changes** — same sections, same ordering, same chart
placement every run. Only the numbers, findings, and recommendations differ.

That constraint is the point: it turns every performance investigation into a
comparable, automatable artifact instead of a bespoke write-up.

> **Status: in development.** Phases 0–3b are implemented and tested (ingestion,
> storage, RAG). Analysis, report rendering, and the unified CLI are not built yet —
> see [Roadmap](#roadmap).

---

## What it does

Two ways in, one normalized data model, one report out.

- **Manual ingestion** — you supply a problem description and/or metric values
  (LCP, CLS, INP, FCP, TTFB, Lighthouse scores, transfer sizes).
- **Automated ingestion** — a headless Chromium campaign drives your target pages
  under configurable **device × network × run-count** matrices, measuring Core Web
  Vitals with native `PerformanceObserver` collectors and main-thread counters
  straight from CDP.

Both converge on the same canonical `Run` object, which is validated by Pydantic,
persisted to SQLite, and used to build a retrieval query against a curated knowledge
base of performance playbooks.

```
  manual input ─┐
                ├─► normalize (Pydantic) ─► SQLite (runs + vectors) ─► RAG ─► analysis ─► report
  browser run ──┘                                                                   (Phase 4-5)
```

---

## Requirements

| | |
|---|---|
| Python | 3.11+ (CI runs 3.13) |
| Browser | Chromium, installed via Playwright |
| API key | Google AI (free tier) — for embeddings; only needed for the RAG layer |

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows;  source .venv/bin/activate  elsewhere
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env             # then fill in GOOGLE_API_KEY
```

`.env` is gitignored and must stay that way — CI fails if it is ever tracked.

---

## Configuring what gets tested

Four files in `config/`, all validated on load with clean error messages:

| File | Purpose |
|---|---|
| `targets.yaml` | Named pages + the per-page test matrix |
| `devices.yaml` | Device presets (viewport, DPR, UA, CPU throttle) |
| `networks.yaml` | Throttling presets (`online`, `fast-3g`, `slow-4g`, `slow-3g`, `offline`) |
| `settings.yaml` | Thresholds, model choices, run defaults, storage paths |

A target is a named page with a list of conditions. One **run** = one page × one
condition, repeated N times with the median reported.

```yaml
# config/targets.yaml
project: storefront
pages:
  - name: homepage
    url: https://example.com/
    tests:
      - { device: mid-mobile, network: slow-4g, runs: 3 }
      - { device: desktop,    network: fast-3g, runs: 3 }
  - name: plp
    url: https://example.com/category/shoes
    # omit `tests` to get the default: one mobile + one desktop condition
```

Unknown device or network names are rejected at load time, naming the offending page.

---

## Running it

### Automated campaign

```bash
python -m ingest.automated                          # the full configured matrix
python -m ingest.automated --pages homepage,plp     # only named pages
python -m ingest.automated --device desktop --runs 5
python -m ingest.automated --dry-run                # print the resolved matrix, no browser
```

`--device`, `--network`, and `--runs` override every condition for that invocation,
so you can explore without editing YAML. One normalized run JSON is written per
(page × condition) to `--output-dir` (default `data/processed`), with HAR, trace, and
screenshot artifacts under `--artifacts-root` (default `data/raw`).

### Manual ingestion

```bash
python -m ingest.manual \
  --page-url https://example.com/ \
  --problem "Homepage LCP spikes to 6s on 3G after the new hero video" \
  --lcp-ms 6200 --cls 0.42 --inp-ms 480 \
  --output data/processed/homepage-manual.json
```

Units and ranges are enforced — out-of-range values are rejected with a clear error
rather than silently stored.

---

## How measurement works

The details that make the numbers trustworthy:

- **Collectors install before navigation.** LCP, CLS, and FCP are *buffered*
  observer entries; an observer attached after `load` misses the very entries being
  measured. Same for CDP counters, which only accumulate while the domain is enabled.
- **LCP settles before any interaction**, because LCP freezes at the first user input.
- **INP is measured, not assumed.** A lab page load contains no interaction, so the
  runner drives a synthetic one — Escape, plus a click on a point *proven*
  non-interactive. On a page with no handlers at all, interactions resolve faster than
  the Event Timing API's 16 ms threshold and no entry is emitted; the run then fails
  validation rather than reporting the floor as if it were a measurement. **TBT** is
  therefore also collected as the always-available lab responsiveness metric.
- **Lighthouse is opt-in.** A faithful programmatic audit needs a Node process wired to
  the page's CDP websocket. CDP already yields the same main-thread breakdown natively,
  so Lighthouse category scores are populated only if you inject `run_lighthouse_fn`.
- **Median of N.** Default 3 runs per condition; every run's raw artifacts are kept so
  results stay auditable.

---

## The RAG layer

`data/knowledge/` holds curated markdown playbooks — one per fix category (images,
fonts, JavaScript, caching, layout shift) with expected-impact ranges and trade-offs.
`rag/` embeds them and retrieves the relevant ones for a given run:

1. `retrieve.detect_symptoms` applies threshold rules to a run ("LCP above target",
   "media-dominated transfer") — no LLM involved.
2. `retrieve.build_query` turns metrics plus symptoms into a prose query.
3. `retrieve.retrieve_context` runs exact top-k cosine search over the embedded chunks.
4. `prompt.build_analysis_prompt` assembles a grounded prompt with retrieved content
   delimited and neutralized as untrusted reference material.

Embeddings go through the Google AI API (`text-embedding-004`) with retry/backoff on
quota errors and a **content-addressed cache**, so unchanged playbooks cost no API
calls on re-index. Missing key and quota-exhausted both raise typed, actionable errors.

This layer is currently a library — there is no `rag` CLI entry point yet; it is wired
up in Phase 4.

### Why not ChromaDB

Vectors are `float32` BLOBs in the *same* SQLite database as the runs, searched with
one `matrix @ vector` in numpy. ChromaDB was removed before any code depended on it: it
carries an unpatched pre-auth RCE (`CVE-2026-45829`, CVSS 10.0) in a server stack this
design never starts, and pulls in 84 packages to deliver *approximate* search where
exact search is both affordable at this corpus size and required by the determinism
rule. Full reasoning in [PROJECT_SPEC.md §8.1](docs/PROJECT_SPEC.md).

---

## Testing

```bash
pytest -m "not e2e"      # 311 offline tests, no browser, no network
pytest -m e2e            # real Chromium against live pages
```

The Playwright surface and every metric collector are injected, so the offline suite
runs entirely against fakes. CI additionally enforces ≥80% coverage, runs `pip-audit`
on pinned dependencies, and runs `gitleaks` over both the working tree **and the full
commit history** — a secret committed and later removed is still a leaked secret.

---

## Security

Documented in full in [SECURITY_PLAN.md](docs/SECURITY_PLAN.md). The controls that
affect day-to-day use:

- **SSRF gate.** Every URL passes `normalize.url_safety.validate_url(resolve=True)`
  *before* any navigation, rejecting non-HTTPS, raw-IP, userinfo, and private/internal
  ranges.
- **Secrets.** API keys live only in gitignored `.env`; `.env.example` holds
  placeholders. Nothing hard-codes or logs a key.
- **Artifacts.** HAR and trace files may contain cookies and tokens. They are recorded
  with response bodies omitted, written under `data/`, and `*.har` / `*.trace.zip` are
  gitignored. Treat them as sensitive.
- **Prompt injection.** Retrieved knowledge is treated as untrusted: delimited from
  instructions, neutralized, and never placed in the system block.
- **No suppressions.** CI fails on a real advisory rather than passing via
  `--ignore-vuln`. When an advisory has no fixed release, the dependency is replaced.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundations — config, CI, secrets hygiene | Done |
| 1 | Canonical schema + manual ingestion | Done |
| 2 | Automated multi-page browser campaigns | Done |
| 3 | SQLite run store + artifact persistence | Done |
| 3b | RAG — embeddings, knowledge base, retrieval, prompts | Done |
| 4 | Analysis — findings, impact, improvement estimator | Planned |
| 5 | Report rendering — fixed HTML skeleton → PDF + Markdown mirror | Planned |
| 6 | Unified CLI (`ingest` / `analyze` / `report`) + skeleton-drift check | Planned |
| 7 | Prior-run memory, trend comparison, optional web UI | Planned |

## Documentation

- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) — full specification and design decisions
- [SECURITY_PLAN.md](docs/SECURITY_PLAN.md) — threat model and controls
- [TESTING_PLAN.md](docs/TESTING_PLAN.md) — test strategy
