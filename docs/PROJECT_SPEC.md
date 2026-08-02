# Performance RAG & Reporting System — Project Specification

> **Doc status:** Draft v0.1 · **Created:** 2026-01-08
> **Owner:** m.parisi
> **Scope:** Design + phased implementation plan for a retrieval-augmented (RAG) system that
> ingests web performance data (manually and automatically), analyzes it, and always emits a
> **consistent PDF report skeleton** containing the same structure but project-specific content.

---

## 1. Executive summary

We are building a **performance-analysis platform** that lets a team capture web performance
data from different sources, store it, have an LLM reason over it (with retrieval over a
curated knowledge base), and produce a **standardized, repeatable PDF report**.

The single most important requirement: **the report "skeleton" never changes** — same sections,
same ordering, same charts layout — only the *numbers, findings, and recommendations* change per
run. This turns every performance investigation into a comparable, automatable artifact.

Two ways to get data into the system:
1. **Manual ingestion** — the user supplies text (problem description) and/or metric values
   (LCP, CLS, INP, FCP, TTFB, Lighthouse scores, bundle sizes, network timings, etc.).
2. **Automated ingestion** — the agent opens the target website in a headless browser,
   applies **mobile emulation + network throttling**, runs performance measurements
   (Lighthouse/CWV, network waterfall, resource timings), and captures the results itself.

---

## 2. Goals / Non-goals

### Goals
- Capture performance info from **multiple input types** into one normalized data model.
- Support **both** manual (typed/CLI/JSON) and **automated** (browser-based) data collection.
- **Multi-page** automated testing: user defines several named pages (e.g., `homepage`, `pdp`,
  `plp`), each with its own list of test *conditions* (device × network) and run counts.
- Full control over **device + throttling/network** per test (like Chrome DevTools presets), with a
  sensible default of **one mid-level-mobile + one desktop** test per page.
- Store data in a way that supports **RAG**: embeddings (via **Google AI API**, free tier) for
  free-text + structured fields.
- Use retrieved knowledge (best practices, prior findings, fix playbooks) to ground analysis.
- Generate an **always-identical-skeleton** PDF (and an MD mirror) with:
  - what/where the problems are,
  - what they cause (impact),
  - what improvements to make,
  - what improvement magnitude to expect.
- Keep every historical run reproducible (same inputs → same report structure, comparable).

### Non-goals (v1)
- Full auth/multi-tenant SaaS UI. (A local CLI/web tool is enough to start.)
- Automating *every* possible metric. We standardize on CWV + Lighthouse + network basics first.
- Real-time alerting (later).

---

## 3. High-level architecture

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                      INGESTION LAYER                     │
                    │                                                         │
    MANUAL INPUTS   │  text problem desc   +  metric values (CLS/LCP/INP/...)  │
    (stdlib/CLI or  │        │                        │                        │
     JSON files /   │        ▼                        ▼                        │
     optional ui)   │  validate       ──►   normalize to unified schema        │
                    │                                                         │
    AUTOMATED       │  Playwright/Chromium with mobile emulation + throttling  │
    BROWSER TEST    │        │                                                │
                    │        ▼                                                │
                    │  Lighthouse (CWV) · network waterfall · resource timings │
                    └────────────────────────┬────────────────────────────────┘
                                             │  normalized measurement set (JSON)
                                             ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │                      STORAGE LAYER                       │
                    │                                                         │
                    │  Structured DB (runs, metrics, project meta)             │
                    │  Vector DB (embeddings of: problem text, findings, KB)   │
                    │  Object store / fs (raw captures, screenshots, har)      │
                    └────────────────────────┬────────────────────────────────┘
                                             │
                                             ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │                      RAG & ANALYSIS LAYER                │
                    │  embed prompt/queries → retrieve KB + prior findings     │
                    │  LLM: locate problem · deduce root cause · propose fix    │
                    │  magnitude estimator (metric deltas)                     │
                    └────────────────────────┬────────────────────────────────┘
                                             │  structured "Report Data Model" (JSON)
                                             ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │                    REPORT GENERATION                    │
                    │  Deterministic template (HTML skeleton ⇢ PDF via headless)
                    │  charts rendered from metric data · same layout every run│
                    │  also emit .md mirror                                    │
                    └────────────────────────┬────────────────────────────────┘
                                             ▼
                                   performance-projects/data/reports/<run-id>/
```

## 4. Input types & unified data model

### 4.1 Ingestion channels
| Channel | Mechanism | Example |
|---|---|---|
| Manual — text | Free-form problem statement (CLI arg or JSON `problem.description`) | "Homepage LCP spikes to 6s on 3G after the new hero video" |
| Manual — metrics | Key/value metrics; auto-validation against expected units | `lcp_ms=6200`, `cls=0.42`, `inp_ms=480`, `fcp_ms=3100` |
| Manual — Lighthouse scores | Optional pre-computed scores from the user | `performance=54`, `accessibility=88`, `seo=90` |
| Automated — CWV | Browser run under throttling, raw web-vitals values | collected automatically |
| Automated — Lighthouse | Programmatic Lighthouse audit via CDP | category scores + audits |
| Automated — network | Waterfall/resource timings, transfer sizes, request counts | collected automatically |
| Automated — screenshots/HAR | Visual overlays and raw capture for the report appendix | png + .har |

### 4.2 Canonical run object (the normalized payload)
Every run (manual or automated) converges to this schema; the report generator consumes it.

```jsonc
{
  "run_id": "run_20260108_1430_ab12",
  "project": { "name": "storefront", "url": "https://example.com" },
  "page": { "name": "homepage", "url": "https://example.com/" },
  "condition": { "device": "mid-mobile", "network": "slow-4g", "cpu_throttle": 4, "runs": 3 },
  "meta": { "created_at": "2026-01-08T14:30:00Z", "source": "automated|manual|mixed", "runner": "cli-1.0" },
  "problem": {
    "description": "free text from user or auto-inferred",
    "keywords": ["lcp", "hero-video", "3g"]
  },
  "metrics": {
    "cwp": {
      "lcp_ms": 6200, "cls": 0.42, "inp_ms": 480,
      "fcp_ms": 3100, "ttfb_ms": 1800, "tbt_ms": 620,
      "target_lcp_ms": 2500, "target_cls": 0.1, "target_inp_ms": 200
    },
    "lighthouse": { "performance": 54, "accessibility": 88, "best_practices": 79, "seo": 90 },
    "network": { "total_transfer_kb": 4820, "request_count": 118, "render_blocking_css": 6 },
    "main_thread": {
      "script_ms": 1820, "layout_ms": 240, "style_ms": 90, "task_ms": 3100,
      "js_heap_kb": 24500, "dom_nodes": 3200, "layout_count": 42,
      "js_event_listeners": 380, "resource_count": 118
    }
  },
  "resource_timings": [ { "name": "/hero.mp4", "type": "media", "transfer_kb": 2140, "duration_ms": 390 } ],
  "captures": { "screenshot": "file.png", "har": "capture.har", "trace": "trace.json" }
}
```

> **Note:** a `run` = **one page × one test condition**. A full *campaign* over many pages ×
> conditions produces **multiple runs** (many rows), which the report layer groups by page for
> charting and comparison.

### 4.3 Validation rules
- Units enforced (ms vs s, ratios 0–1 vs %). Reject out-of-range values with a clear error.
- Required CWV trio for automated runs: **LCP, CLS, INP** (plus FCP/TTFB when available).

> **INP in a lab run (measured, not assumed).** INP only exists once a real
> interaction has happened, so the runner drives a *synthetic* one (Escape key +
> a click on a point proven non-interactive) after LCP settles. On a page with
> real handlers this yields a true latency (measured: a 120 ms handler reports
> INP ≈ 128 ms, stable across runs). On a page with **no** event handlers at all
> — e.g. `example.com` — interactions resolve faster than the Event Timing API's
> 16 ms `durationThreshold` floor, so no entry is emitted and the run fails
> validation rather than reporting the floor as if it were a measurement.
> **TBT** (`tbt_ms`) is therefore also collected as the always-available lab
> responsiveness metric, derived from long tasks after FCP.
- Each run must reference a `page` (named, with url) and a `condition` (device + network + runs)
  so reports are comparable per page and per condition.

### 4.4 Multi-page test matrix (how the user controls testing)

The user configures **named pages**, and for each page a **list of test conditions**. Each
condition = a **device** choice × a **network/throttle** choice × a **run count**. This mirrors
Chrome DevTools' device + throttling presets.

```yaml
# config/targets.yaml
project: storefront
pages:
  - name: homepage
    url: https://example.com/
    tests:                     # one entry per simulation the user wants
      - { device: mid-mobile, network: slow-4g, runs: 3 }   # default mobile
      - { device: desktop,    network: fast-3g, runs: 3 }   # default desktop
  - name: pdp
    url: https://example.com/p/123
    tests:
      - { device: mid-mobile, network: slow-4g, runs: 3 }
      - { device: desktop,    network: online,  runs: 1 }
  - name: plp
    url: https://example.com/category/shoes
    tests:                     # e.g., test many conditions on the busiest page
      - { device: mid-mobile, network: slow-3g, runs: 3 }
      - { device: high-mobile, network: slow-4g, runs: 1 }
      - { device: desktop,    network: fast-3g, runs: 2 }
      - { device: desktop,    network: online,  runs: 1 }
```

**Devices** (`config/devices.yaml`) — resolved to Playwright descriptors / CDP emulation:
`mid-mobile` (default "mid-level phone", e.g. Pixel 5-class viewport + 4× CPU throttle),
`high-mobile` (flagship class + 2× CPU), `low-mobile` (budget class + 6×), `desktop`, or any
named device (e.g. `iPhone 13`, `Pixel 7`) passed through to Playwright.

**Networks / throttling** (`config/networks.yaml`) — the presets you see in DevTools:
`online`, `fast-3g`, `slow-4g`, `slow-3g`, `offline`, `custom` (latency / downlink / uplink in
ms & Mb/s). Applied via CDP `Network.emulateNetworkConditions` + CPU throttle.

**Defaults:** if `tests` is omitted for a page, the system uses **mid-mobile@slow-4g** and
**desktop@fast-3g** (the "one mobile + one desktop per page" default).

**CLI override:** `--device`, `--network`, `--runs`, and `--pages <name,...>` let the user
override the matrix at run time without editing the file.

## 5. RAG design & knowledge base

### 5.1 What gets embedded (indexed into vector DB)
1. **Curated improvement knowledge base** (`data/knowledge/`) — markdown playbooks: one file per
   fix category (image optimization, font loading, code splitting, caching, LCP/CLS/INP tactics),
   each with expected-impact ranges and trade-offs.
2. **Prior run findings** — each completed run's findings text is embedded so future runs can
   "remember" what was tried and what worked.
3. **Project/problem text** — free-text problem descriptions for similarity-based lookup.

### 5.2 Retrieval flow
- On each analysis, construct a query from: current metrics (as prose), the problem description,
  and detected symptoms (e.g., "large media", "many requests", "render-blocking CSS").
- Retrieve top-k most relevant KB playbooks + related prior findings.
- Feed retrieved context into the LLM as **grounding**, instructing the model to cite the
  specific playbook(s) it used.

### 5.3 Optional additions (stretch)
- Web-ROM or MDN/CWV docs as extra embedding sources.
- Embed the metric → playbook mapping so "LCP high + media heavy" reliably retrieves the
  *media* playbook rather than the *fonts* one.

---

## 6. The fixed report skeleton (the core requirement)

The PDF must have the **identical structure every time**; only data changes.

| # | Section | Content type | Source |
|---|---|---|---|
| 0 | Cover page | Project, run id/date, tested pages, overall verdict badge | meta + scores |
| 1 | Executive summary | Problem · key finding · top 3 actions | LLM (grounded) |
| 2 | Key metrics dashboard | Score table + CWV gauges (LCP/CLS/INP against targets) | metrics |
| 3 | Where the problem is | Waterfall / resource-size bar charts, per-phase LCP breakdown | resource_timings |
| 4 | What it causes (impact) | Readable impact statements (UX, SEO, conversion proxies) | LLM + stats |
| 5 | Improvements | Ordered recommendations, each with effort & expected magnitude | LLM + KB |
| 6 | Expected improvements | Before/after projected chart (metric delta) per recommendation | estimator |
| 7 | Methodology & appendix | Device/throttle profile, capture list, raw screenshots | captures |

### 6.1 Chart inventory (rendered from data, same placement each run)
Reports span all tested pages. Section **2/3** render the CWV dashboard and charts **once per
page** in a fixed repeating block (page name header), so the skeleton stays constant while the
content scales with the page count.
- Per-page CWV gauges / score cards (LCP, CLS, INP vs targets)
- Per-page resource transfer-size bar chart (top offenders)
- Per-page request-type donut (img / media / css / js / font / other)
- Per-page LCP phase breakdown (TTFB, resource load, element render)
- Before→after projected bars (aggregated + per recommendation)
- Cross-page comparison table (page × condition → key metric) for the summary

### 6.2 Determinism rules
- Report JSON is authored first; HTML template only transforms it.
- No LLM word-randomness in section *layout*; only in *content prose*.
- Charts use fixed palettes (pass = green, warn = amber, fail = red) computed from thresholds.

## 7. Automated browser testing (detail)

- Use the Anthropic **`webapp-testing`** skill: native Python Playwright scripts with a
  `with_server`-style lifecycle; reconnaissance-then-act workflow.
- **Scan loop (campaign):** for each configured page and each of its test conditions, run the
  simulation N times. Each (page × condition) produces one normalized run (see §4.2).
- **Device emulation:** resolve `page.tests[].device` from `config/devices.yaml` to a Playwright
  device descriptor or a CDP-emulated viewport (DPR, UA, touch, screen size). Default mid-mobile
  uses a mid-range phone profile + 4× CPU throttle.
- **Network / throttling:** resolve `page.tests[].network` from `config/networks.yaml` to CDP
  `Network.emulateNetworkConditions` (latency / downlink / uplink) — the same presets as Chrome
  DevTools — optionally combined with CPU throttle.
- **Metrics per run (CDP-direct, decision #4):** Core Web Vitals via native
  `PerformanceObserver` collectors installed **before navigation** (LCP/CLS/FCP are buffered
  entries and are lost if attached after `load`), plus DevTools main-thread counters read over
  CDP `Performance.getMetrics` (script / layout / style / task time, DOM nodes, heap, listeners).
  The domain is enabled **before navigation** — counters only accumulate while enabled.
  Capture HAR (bodies omitted), trace, and a screenshot for the appendix.
- **Why not the Lighthouse Node bridge:** a faithful programmatic Lighthouse audit needs a Node
  process wired to the page's CDP websocket. CDP gives the same main-thread data natively with no
  extra runtime, so Lighthouse category scores are **opt-in** (inject `run_lighthouse_fn`) rather
  than required; the schema treats them as optional.
- **Reproducibility:** N runs per condition (default 3), report the **median**; store every run's
  raw artifacts so results are auditable.
- **Budget guardrails:** request caps, navigation timeouts, and a per-page target list so long
  campaigns can't hang.

---

## 8. Tech stack (recommended)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ (core), Node optional | Playwright + Lighthouse + reportlab ecosystem |
| Browser automation | Playwright (Python) + CDP | emulation + throttling + HAR capture |
| Performance metrics | Lighthouse (via CDP) + web-vitals | industry-standard CWV |
| Structured storage | SQLite (or DuckDB) | zero-config, queryable |
| Vector DB | **SQLite BLOBs + numpy exact search** (decision #6) | See §8.1 — ChromaDB dropped. Zero new dependencies, exact (not approximate) retrieval, same file as the run store. |
| Embeddings | **Google AI API** — free tier, e.g. `text-embedding-004` | API embeddings (decision #1); key via `.env` |
| Secrets / env | `python-dotenv` + `.env` (gitignored) + `.env.example` | keeps the shared Google API key out of git |
| LLM orchestration | **`google-genai` directly** (decision #5) | LiteLLM dropped: it requires `tokenizers>=0.21` while ChromaDB 0.5.x pins `<=0.20.3`, and 1.40.0 carried 19 CVEs. One client for both embeddings and generation. |
| Charts | Matplotlib / Plotly→static | deterministic, embeds in PDF |
| PDF | HTML template → headless Chromium print-to-PDF **or** ReportLab | fixed skeleton via template |
| MD mirror | Jinja2 template | same content, markdown form |
| Report styling | **`frontend-design`** skill (decision #2) | distinctive, production-grade report look |

> **Why HTML→PDF over ReportLab:** a static HTML/CSS skeleton gives precise, consistent layout,
> easy chart embedding, and reuses strong frontend/CSS skills we already have. ReportLab remains
> a fallback for fully scripted output with no browser dependency.

### 8.1 Why we dropped ChromaDB (decision #6)

The spec originally chose ChromaDB as a "lightweight local RAG, no server" vector
store. We removed it before writing any code against it, for three reasons:

1. **An unpatched critical RCE.** `CVE-2026-45829` / `PYSEC-2026-311` ("ChromaToast")
   is a pre-authentication remote code execution flaw (CVSS 10.0) in the server's
   collections endpoint, which processes user-supplied model configs and fetches
   `trust_remote_code` scripts *before* validating auth. It affects 1.0.0 through
   1.5.9 with **no fixed release** as of 2026-08, and the maintainers reportedly did
   not respond to disclosure attempts from late 2025 onward.
2. **We would install the vulnerable component without ever using it.** ChromaDB
   pulls in **84 packages** — including the FastAPI/uvicorn server stack that carries
   the flaw, plus a Kubernetes client, `onnxruntime`, 10 OpenTelemetry packages and
   `posthog` telemetry. Our design never starts a server, so all of that is
   dependency surface with no corresponding benefit.
3. **The workaround costs more than the feature.** Staying on 0.5.x avoids the CVE
   (it predates the affected range) but pins us to an unmaintained line that will
   accumulate its own advisories with no fixes coming. Using 1.x embedded is safe in
   practice — no port is ever bound — but `pip-audit` flags the package regardless,
   so CI could only pass by suppressing a critical advisory. Migrating to the Rust
   `chroma run` server means adopting a server process, which contradicts the
   "no server" requirement that motivated the choice in the first place.

**What we do instead.** Embeddings are stored as `float32` BLOBs in the *same*
SQLite database as the runs, and retrieval is an exact top-k cosine search — one
`matrix @ vector` in numpy (`store/vectordb.py`).

| | ChromaDB | SQLite + numpy |
|---|---|---|
| New dependencies | 84 packages | 0 (numpy already required by matplotlib) |
| Known critical CVEs | 1, unpatched | 0 |
| Retrieval | approximate (HNSW) | **exact** |
| Stores | separate vector store | same file as runs |

This suits the workload precisely. The corpus is knowledge-base playbook chunks
plus accumulated run findings; at 768 dimensions even 100k chunks is ~300 MB and
~50 ms per query, far beyond what this tool will hold. Exact search also directly
serves the determinism rule in §6.2: identical inputs retrieve identical context,
where an approximate index can silently return different neighbours after a rebuild
or parameter change.

`SqliteVectorStore` implements the `VectorStore` protocol, so if the corpus ever
outgrows brute force, a LanceDB-backed implementation (embedded, ANN, no server)
drops in without touching the `rag/` layer.

## 9. Proposed directory structure

```
performance-projects/
├─ docs/
│  └─ PROJECT_SPEC.md              # this document
├─ config/
│  ├─ settings.yaml                # thresholds, model choices, run defaults (runs=3, etc.)
│  ├─ targets.yaml                 # named pages + per-page test matrix (see §4.4)
│  ├─ devices.yaml                 # device presets (mid-mobile, high-mobile, desktop, ...)
│  └─ networks.yaml                # throttle presets (online, fast-3g, slow-4g, ...)
├─ src/
│  ├─ ingest/
│  │  ├─ manual.py                 # CLI/JSON ingestion + validation
│  │  ├─ automated.py              # orchestrates browser run
│  │  └─ browser/
│  │     ├─ runner.py              # Playwright lifecycle + emulation/throttling
│  │     ├─ lighthouse.py          # Lighthouse over CDP
│  │     └─ webser.py              # web-vitals + network capture helpers
│  ├─ normalize/
│  │  └─ schema.py                 # canonical run object → Pydantic model + validators
│  ├─ store/
│  │  ├─ sql.py                    # SQLite schema + queries (runs, metrics)
│  │  ├─ vectordb.py               # embeddings in SQLite + exact cosine search (§8.1)
│  │  └─ artifacts.py              # raw capture files (png/har/trace/json)
│  ├─ rag/
│  │  ├─ knowledge.py              # loads/embeds data/knowledge/ playbooks
│  │  ├─ retrieve.py               # build query, top-k retrieval
│  │  └─ prompt.py                 # grounded prompt templates
│  ├─ analysis/
│  │  ├─ llm.py                    # model client (LiteLLM/LangChain)
│  │  ├─ findings.py               # problem localization + impact statements
│  │  ├─ estimator.py              # expected-improvement magnitude (metric deltas)
│  │  └─ reportmodel.py            # emits Report JSON consumed by report layer
│  ├─ report/
│  │  ├─ charts.py                 # Matplotlib chart builders (fixed palette)
│  │  ├─ template/                 # HTML skeleton + CSS (fixed layout)
│  │  │  ├─ report.html.j2
│  │  │  └─ style.css
│  │  ├─ render_pdf.py             # headless Chromium print-to-PDF
│  │  └─ render_md.py              # Jinja2 → Markdown mirror
│  └─ cli.py                       # entrypoint: ingest → analyze → report
├─ data/
│  ├─ knowledge/                   # curated markdown playbooks (embedded)
│  ├─ raw/                         # per-run captures
│  ├─ processed/                   # normalized run JSON
│  │                               # (embeddings live in the runs SQLite db)
│  └─ reports/                     # generated PDF + MD outputs
├─ tests/
│  ├─ unit/                        # schema validation, estimator, charts
│  └─ e2e/                         # ingest→report happy path + determinism check
├─ pyproject.toml / requirements.txt
├─ .env.example                    # template with placeholders — commit this
├─ .env                            # real secrets (Google API key) — NEVER commit (gitignored)
├─ .gitignore                      # ignores .env, data/vector, data/raw, data/reports, __pycache__
└─ README.md
```

### 9.1 Secrets & `.env` (Google API key hygiene)
- The **Google AI API key** (free tier) is the only shared secret; it must **never** reach git even
  though the code is shared.
- `.env.example` (committed) contains **placeholders only**, e.g.:
  ```env
  GOOGLE_API_KEY=your_google_api_key_here
  GEMINI_MODEL=gemini-2.0-flash          # generation model (optional/pluggable)
  EMBEDDING_MODEL=text-embedding-004     # Google embeddings model
  ```
- Each developer copies `.env.example` → `.env` and fills in their own key.
- `.gitignore` must include `.env` (and `data/vector`, `data/raw`, `data/reports`). A CI
  guard/`pre-commit` step can fail if `.env` ever appears in `git status`.
- Code loads secrets via `python-dotenv`; nothing hard-codes the key.

---

## 10. Development phases (steps before we start coding the real system)

### Phase 0 — Foundations
- [x] Set up Python 3.11 virtual env + `requirements.txt` (playwright, numpy,
      matplotlib, jinja2, pydantic, python-dotenv, typer/click, reportlab fallback, sqlite driver,
      google-genai).
- [x] `playwright install chromium`; verify mobile emulation + CDP throttling on a sample site.
- [x] Create `.gitignore` (ignore `.env`, `data/vector`, `data/raw`, `data/reports`, `__pycache__`)
      and `.env.example` (placeholders) — confirm `git status` shows no secrets.
- [x] Author `config/settings.yaml`, `config/devices.yaml`, `config/networks.yaml` (presets),
      and a sample `config/targets.yaml` with named pages + default mobile/desktop matrix.

### Phase 1 — Data model & manual ingestion
- [x] Implement `normalize/schema.py` (Pydantic) for the canonical run object (Section 4.2),
      including `page`, `condition`, and multi-run grouping.
- [x] Implement `ingest/manual.py` CLI: accept problem text + metric pairs + optional scores; validate.
- [x] Write unit tests for validation (units, ranges, required fields).

### Phase 2 — Automated browser testing (multi-page matrix)
- [x] Implement `config` loaders for `targets.yaml` / `devices.yaml` / `networks.yaml`; resolve each
      named page into its list of (device × network × runs) test conditions (§4.4).
- [x] Implement `ingest/browser/runner.py`: Playwright lifecycle, device emulation, throttle
      presets (CDP latency/downlink/uplink + CPU throttle), HAR/trace/screenshot.
- [x] Implement `ingest/browser/lighthouse.py` (Lighthouse over CDP) + `webser.py` (web-vitals).
- [x] Implement `ingest/automated.py` campaign loop: for each page & condition, run N times, take
      median, emit one normalized run per (page × condition).
- [x] Support CLI overrides `--device`, `--network`, `--runs`, `--pages`.
- [x] E2E: capture a real site across 2 pages × (mid-mobile, desktop); assert complete run JSONs.

### Phase 3 — Storage + RAG (Google AI embeddings)
- [x] Implement `store/sql.py` (SQLite) and `store/artifacts.py` (file persistence).
- [x] Implement `store/vectordb.py` (SQLite + numpy exact search, §8.1); `rag/knowledge.py` embeds via **Google AI API**
      (`text-embedding-004`) with key loaded from `.env` (never hard-coded).
- [ ] Author initial `data/knowledge/` playbooks (images, fonts, code-splitting, caching, CWV tactics).
- [ ] Implement `rag/retrieve.py` + `rag/prompt.py`; write tests for retrieval quality.
- [ ] Graceful behavior when API key missing / free-tier quota hit (clear error, retry/backoff).

### Phase 4 — Analysis + report model
- [ ] Implement `analysis/llm.py`, `analysis/findings.py` (localize problem + impact), `estimator.py`.
- [ ] Implement `analysis/reportmodel.py` producing the Report JSON (Section 6).
- [ ] Unit-test estimator math (projected before/after deltas).

### Phase 5 — Report rendering (fixed skeleton, frontend-design styling)
- [ ] Use the **`frontend-design`** skill to establish the report's visual identity (palette,
      typography, layout) while keeping a strict, reusable template.
- [ ] Build `report/template/report.html.j2` + `style.css` matching Section 6 layout exactly,
      with a fixed per-page repeating block for multi-page reports.
- [ ] Implement `report/charts.py` (fixed palette + locations) and `render_pdf.py` (Chromium print-to-PDF).
- [ ] Implement `render_md.py` (Jinja2 → Markdown mirror).
- [ ] Determinism test: two campaigns with same data → identical structure, layout, charts.

### Phase 6 — CLI orchestration + polish
- [ ] Wire `src/cli.py`: `ingest` (manual/auto), `analyze`, `report`, `list-runs`.
- [ ] Add `--skeleton-check` mode that verifies report structure did not drift.
- [ ] End-to-end run producing a real `data/reports/<run-id>/report.pdf` + `.md`.
- [ ] Write README with usage examples.

### Phase 7 — Hardening / stretch (post-MVP)
- [ ] Prior-run memory in RAG; trend-over-time comparison; PDF appendix with screenshots + HAR.
- [ ] Optional lightweight web UI (reuse frontend/CSS skills) for manual entry.
- [ ] CI to auto-regenerate reports and catch skeleton drift.

## 11. Risks & open questions

| Risk / question | Mitigation / decision needed |
|---|---|
| "Always same skeleton" drift as LLM content changes | Render from deterministic **Report JSON**; template only formats. Add `--skeleton-check`. |
| LLM hallucinated improvement magnitudes | Ground every magnitude in KB playbooks with explicit ranges; estimator applies rule-based deltas, not free-form claims. |
| Testing external live sites (CORS, auth, redirects) | Start with public pages; document auth/login bypass as a known limitation. |
| Mobile emulation ≠ real device | Note in methodology that results are emulated proxies, comparable run-to-run. |
| Throttling variance between runs | Median of N runs; fixed device/throttle profile per run id. |
| **Google API key leak / hard-coding** | Key only in `.env`, gitignored; `.env.example` has placeholders; `pre-commit`/CI guard fails if `.env` in `git status`. |
| **Google free-tier quota limits** | Retry/backoff on 429, cache embeddings, graceful "quota hit" error. Batch embedding to stay under limits. |
| **Embeddings choice (RESOLVED → API)** | **Decided:** Google AI API free tier (`text-embedding-004`). Key via `.env`. |
| **Report styling (RESOLVED → frontend-design)** | **Decided:** use the `frontend-design` skill for a distinctive, production-grade report template. |
| **Target scope (RESOLVED → multi-page matrix)** | **Decided:** named pages (homepage/pdp/plp) each with a configurable device × network × runs matrix, defaulting to mid-mobile + desktop. |

---

## 12. Skills in use (installed) + how they map

| Skill | Where it helps |
|---|---|
| `webapp-testing` (Anthropic) | Automated browser ingestion — mobile emulation + throttling + metrics. |
| `pdf` (Anthropic) | PDF generation / ReportLab fallback; PDF editing if needed. |
| `vector-databases` | RAG sizing reference; we implement exact search over SQLite (§8.1). |
| `frontend-design` | Report HTML/CSS skeleton styling (Phase 5). |
| `vercel-optimize` | Grounding info for performance recommendations in the KB. |
| `vercel-react-best-practices` / `composition-patterns` | KB content for React/Next performance recommendations. |
| `tanstack-query` | KB content for data-fetching/caching performance recommendations. |
| `seo-technical` / `semantic-html-and-seo` | KB content for SEO-impact sections of the report. |
| `shadcn` | Optional lightweight web UI for manual data entry (Phase 7 stretch). |

---

## 13. Task breakdown for parallel work (divide & conquer)

Assign owners; each task is mostly independent after Phase 0.

1. **Owner A — foundational plumbing:** Python env, `requirements.txt`, `.gitignore` + `.env.example`,
   Playwright install, Pydantic schema (`normalize/schema.py` with `page` + `condition`),
   `config/settings.yaml`, `config/devices.yaml`, `config/networks.yaml`.
2. **Owner B — manual ingestion:** `ingest/manual.py` CLI + validation + its unit tests.
3. **Owner C — browser testing (multi-page matrix):** config loaders for `targets.yaml`/`devices.yaml`/
   `networks.yaml`, `ingest/browser/*` (runner, lighthouse, web-vitals), campaign loop + CLI overrides
   (`--device/--network/--runs/--pages`).
4. **Owner D — storage:** `store/sql.py`, `store/artifacts.py`, `store/vectordb.py`.
5. **Owner E — knowledge base + RAG:** author `data/knowledge/` playbooks, `rag/*` (Google AI embeddings
   via `.env` key), retrieval tests, quota/backoff handling.
6. **Owner F — analysis:** `analysis/llm.py`, `findings.py`, `estimator.py`, `reportmodel.py`.
7. **Owner G — report rendering:** `report/charts.py`, HTML template + CSS (via `frontend-design`),
   `render_pdf.py`, `render_md.py`.
8. **Owner H — CLI + integration:** `src/cli.py`, determinism test, end-to-end multi-page run, README.

> **Hard dependencies:** B & C depend on A. D depends on A. E depends on D. F depends on E (+C data).
> G depends on A + F's Report JSON. H depends on everyone → integrate last, in two integration waves
> (wave 1: A→B/C/D; wave 2: E→F→G→H).

---

## 14. Definition of done (MVP)

- [ ] `ingest` works for **manual** (text + metrics) and **automated** (multi-page browser campaign
      with configurable device × network × runs matrix + CWV/Lighthouse).
- [ ] `.env` holds the Google API key; nothing secret is committed (`.gitignore` verified).
- [ ] Data lands in SQLite + Vector DB; KB playbooks embedded via Google AI API and retrievable
      (graceful on quota/key-missing).
- [ ] `analyze` produces a grounded Report JSON with where/cause/improvements/expected-magnitude,
      spanning all tested pages.
- [ ] `report` emits a **consistent, fixed-skeleton** PDF + Markdown mirror (frontend-design styling)
      with per-page charts.
- [ ] Determinism check passes: same data → same structure/layout every time.
- [ ] Test suite green; a real multi-page sample report generated and reviewed.

---

*End of specification v0.1. Next action after review: address open questions in §11, then start **Phase 0***.





