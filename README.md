# Performance RAG & Reporting System

Capture web performance data, reason over it with a retrieval-grounded LLM, and emit a
**report whose skeleton never changes** — same sections, same ordering, same chart
placement every run. Only the numbers, findings, and recommendations differ.

That constraint is the point: it turns every performance investigation into a
comparable, automatable artifact instead of a bespoke write-up.

## Where the project is

**The pipeline is end to end and driven from one command: measurements go in, a
fixed-skeleton PDF comes out.**

**Working today (phases 0–6, plus 7a–7b):** config and test-matrix resolution · canonical
Pydantic schema · manual ingestion CLI · automated multi-page browser campaigns with
device and network emulation · SSRF-gated navigation · SQLite run store with scrubbed
artifacts · RAG over the knowledge base (embeddings, chunking, symptom detection,
grounded prompts) · optional per-target request headers for bot-protected sites ·
grounded per-page analysis with rule-based improvement projections · a fixed-skeleton
PDF, HTML and Markdown mirror rendered from the Report JSON · a unified `python -m cli`
entry point and a `--skeleton-check` drift guard enforced against a committed baseline ·
**campaign-over-campaign trends per page and condition** · **a per-capture appendix
embedding each screenshot and its heaviest HAR requests** · **a loopback-only web form
for manual entry** · **a CI job that regenerates a real campaign report and gates its
skeleton**.

**Missing:** nothing — every phase in the [Roadmap](#roadmap) is built. Anything
this README does not describe as working is not there.

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
  browser run ──┘
```

---

## Requirements

|         |                                                                       |
| ------- | --------------------------------------------------------------------- |
| Python  | 3.11+ (CI runs 3.13)                                                  |
| Browser | Chromium, installed via Playwright                                    |
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

| File            | Purpose                                                                   |
| --------------- | ------------------------------------------------------------------------- |
| `targets.yaml`  | Named pages + the per-page test matrix                                    |
| `devices.yaml`  | Device presets (viewport, DPR, UA, CPU throttle)                          |
| `networks.yaml` | Throttling presets (`online`, `fast-3g`, `slow-4g`, `slow-3g`, `offline`) |
| `settings.yaml` | Thresholds, model choices, run defaults, storage paths, browser timeouts  |

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
      - { device: desktop, network: fast-3g, runs: 3 }
  - name: plp
    url: https://example.com/category/shoes
    # omit `tests` to get the default: one mobile + one desktop condition
```

Unknown device or network names are rejected at load time, naming the offending page.

### Timeouts

A heavy commerce page measured under CPU *and* network throttling, while HAR and trace
recording are active, can take far longer to fire `load` than a typical page — so the
per-navigation budgets are configuration rather than constants:

```yaml
# config/settings.yaml
timeouts:
  navigation_ms: 90000    # fatal on expiry: nothing loaded, nothing to measure
  network_idle_ms: 15000  # NOT fatal: `load` already fired, idle is a settle window
  lcp_ms: 5000
  inp_ms: 1000
```

Only `navigation_ms` expiring fails a run. A page with continuous analytics beacons may
never reach network idle, and aborting there would discard the whole campaign — every
condition already measured included — over a wait that was only ever an optimisation.

### Targets behind bot protection (optional)

Sites fronted by a bot filter such as Akamai answer automated traffic with `403`/`429`.
If you have an allowlist token, declare the header name in config and keep the value in
`.env`:

```yaml
project: oo
headers:
  X-Akamai-Bot: ${AKAMAI_BOT_TOKEN} # value resolved from .env, never committed
pages:
  - name: homepage
    url: https://www.oa.com/en-us
```

Headers are applied at the browser-context level, so they cover the document _and_
every sub-resource. This is **fully opt-in**: declare none and nothing changes. Use
`--no-headers` to run without them, and see [CUSTOM_HEADERS.md](docs/CUSTOM_HEADERS.md)
for scoping rules and how to confirm the token was accepted.

---

## Running it

One entry point covers the whole pipeline:

```bash
python -m cli                       # the command table
python -m cli ingest auto           # run a browser campaign
python -m cli list-runs             # what is in the store
python -m cli analyze               # runs  → report.json
python -m cli report                # report.json → HTML + Markdown + PDF
python -m cli ui                    # loopback-only form for manual entry
```

`cli.py` is a façade: it consumes the command and passes every remaining flag
straight to the stage that owns it. So `python -m cli report --help` shows the report
stage's own help, per-command help can never drift from the parser it documents, and
each stage keeps its direct entry point — `python -m analysis --no-llm` is still valid.
The longhand invocations below and their `python -m cli` equivalents are the same code.

### Automated campaign

```bash
python -m cli ingest auto                           # the full configured matrix
python -m cli ingest auto --pages homepage,plp      # only named pages
python -m cli ingest auto --device desktop --runs 5
python -m cli ingest auto --dry-run                 # print the resolved matrix, no browser
python -m cli ingest auto --no-headers              # ignore configured request headers
```

`--device`, `--network`, and `--runs` override every condition for that invocation,
so you can explore without editing YAML. One normalized run JSON is written per
(page × condition) to `--output-dir` (default `data/processed`), with HAR, trace, and
screenshot artifacts under `--artifacts-root` (default `data/raw`).

### Manual ingestion

```bash
python -m cli ingest manual \
  --page-url https://example.com/ \
  --problem "Homepage LCP spikes to 6s on 3G after the new hero video" \
  --lcp-ms 6200 --cls 0.42 --inp-ms 480 \
  --output data/processed/homepage-manual.json
```

Units and ranges are enforced — out-of-range values are rejected with a clear error
rather than silently stored.

### The manual-entry form

```bash
python -m cli ui                    # http://127.0.0.1:8765/
python -m cli ui --port 9000 --output-dir data/processed
```

A browser front door to the same ingestion path: the form posts to
`build_manual_run`, so every unit and range rule in `normalize/schema.py` applies
exactly as it does on the command line, and the run JSON it writes is identical to the
CLI's apart from the run id, the timestamp and `meta.runner`. A test asserts that
parity on every run of the suite. The range limits in the markup are generated from the
Pydantic model rather than typed into the template, so they cannot drift from it, and
the device and network selects start on `settings.run_defaults` — not on whichever
preset happens to be listed first, which would file untouched runs under `online`.

It **serves loopback only**. A `--host` that is not `127.0.0.1`, `localhost` or `::1`
exits with an error rather than a warning: the form has no authentication, and it
writes files. There is nothing to log in to because there is nothing remote to reach
it.

A rejected submission comes back with every value still in it and the message beside
the offending field — losing a filled page to one bad digit is the fastest way to send
someone back to the CLI. No JavaScript, no build step, no new dependency.

### Seeing what you have

```bash
python -m cli list-runs                             # 20 newest stored runs
python -m cli list-runs --pages homepage,plp --limit 50
python -m cli list-runs --device desktop --network fast-3g
```

```
RUN ID                    PAGE      DEVICE      NETWORK  LCP   CLS   INP
homepage-mid-mobile-3f2a  homepage  mid-mobile  slow-4g  4820  0.12  210
plp-mid-mobile-71bd       plp       mid-mobile  slow-4g  6210  0.34  —
```

A metric the run does not carry prints `—`, never `0` — a page with no interaction
handlers emits no INP entry at all, and a missing measurement must not read as a perfect
one. The store path defaults to `settings.storage.sqlite_path`; a `--db` pointing at
nothing is an error rather than an empty listing.

---

## How measurement works

The details that make the numbers trustworthy:

- **Collectors install before navigation.** LCP, CLS, and FCP are _buffered_
  observer entries; an observer attached after `load` misses the very entries being
  measured. Same for CDP counters, which only accumulate while the domain is enabled.
- **LCP settles before any interaction**, because LCP freezes at the first user input.
- **An LCP candidate with no timing cannot become the measurement.** A cross-origin
  resource served without `Timing-Allow-Origin` can produce an LCP entry whose
  `renderTime` *and* `loadTime` are both 0 — seen on a hero `<video>`. That 0 is an
  absence of timing, not a 0 ms paint, so it is never allowed to overwrite a real
  earlier candidate. The reported LCP is then the largest element that *did* report a
  time, `cwp.lcp_underestimated` is set, and the report labels the figure a **lower
  bound** rather than presenting it as the measurement.
- **INP is measured, not assumed.** A lab page load contains no interaction, so the
  runner drives a synthetic one — Escape, plus a click on a point _proven_
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

This layer is a library; it is driven by the analysis layer below.

### Why not ChromaDB

Vectors are `float32` BLOBs in the _same_ SQLite database as the runs, searched with
one `matrix @ vector` in numpy. ChromaDB was removed before any code depended on it: it
carries an unpatched pre-auth RCE (`CVE-2026-45829`, CVSS 10.0) in a server stack this
design never starts, and pulls in 84 packages to deliver _approximate_ search where
exact search is both affordable at this corpus size and required by the determinism
rule. Full reasoning in [PROJECT_SPEC.md §8.1](docs/PROJECT_SPEC.md).

---

## The analysis layer

`analysis/` turns stored runs into the **Report JSON** — the document the report layer
renders. It is the last stage that computes anything; the template only formats.

```bash
python -m cli analyze                                # every run in data/processed
python -m cli analyze --pages homepage,plp
python -m cli analyze --from-store data/processed/runs.sqlite
python -m cli analyze --no-llm                       # rule-based only, no model calls
python -m cli analyze --use-priors                   # ground in earlier campaigns' findings
```

Output lands in `data/reports/<campaign-id>/report.json`. The campaign id is derived
from the run ids, not the clock, so re-analysing the same runs writes the same file.

**One call per page, plus one for the summary.** Each page is analysed at its worst
condition — a recommendation derived from the easy desktop run is the wrong
recommendation — with the other conditions carried through as comparison data.

**The model never supplies a number.** It writes prose and names *which* playbook
justifies each recommendation; there is no numeric field in its output contract. The
magnitude then comes from that playbook's front matter
(`expected_lcp_reduction_pct: 15, 40`), applied by a pure estimator that cannot see the
model's text. A recommendation citing a playbook that was not retrieved is **dropped**,
not repaired, and the drop is counted in `meta.dropped_recommendations`.

Stacked fixes on one metric are discounted (each subsequent fix at 80% of its stated
effect) and capped at 70% total, because the second image fix cannot re-win bytes the
first already removed. Reports show the conservative low bound alongside the playbook's
full band.

**It always produces a report.** No API key, an exhausted free-tier quota, or a model
that returns unusable JSON twice all degrade to a rule-based path: symptoms become
findings, and playbooks are matched by their front-matter `symptoms:` instead of by
embedding. `meta.analysis_mode` and `meta.degradation_reason` state exactly what
produced the document, so a degraded report is never mistaken for a reasoned one.

---

## The report

`report/` renders the Report JSON into the deliverable. It is the last stage that
formats anything and the first that computes nothing — every number, ordering and
verdict was decided by the analysis layer.

```bash
python -m cli report                                # newest campaign in data/reports
python -m cli report --campaign storefront-9f3ab120
python -m cli report --input data/reports/<id>/report.json
python -m cli report --no-pdf                       # HTML + Markdown only, no browser
python -m cli report --no-appendix-images           # path-only rows, no embedded screenshots
python -m cli report --skeleton-check               # fail if the structure drifted
python -m cli report --update-baseline              # accept a structural change
```

Writes `report.html`, `report.md` and `report.pdf` beside the source `report.json`.

**The skeleton is enforced, not hoped for.** Every structural block carries a
`data-section` attribute; `report/skeleton.py` reads them in document order and
collapses the repeating per-page block to a single `page[]` group. The test that
matters compares a **one-page campaign against a three-page campaign** and requires an
identical fingerprint. Diffing one campaign against itself would only prove the
renderer is a pure function — it would pass while a section silently vanished for
every report, which is how a skeleton actually rots. No section is ever conditionally
omitted: a page with no recommendations renders the block with an explicit empty state.

**Every page carries its own history.** Each `(page, device, network)` gets one series
per metric — LCP, CLS, INP, TBT — so a mid-mobile/slow-4g LCP is only ever compared
against other mid-mobile/slow-4g LCPs. Merging conditions would manufacture regressions
out of nothing: a campaign that merely added a desktop condition would show every page
improving.

Series keys come from the *current* campaign, not from the store, so a condition dropped
from `targets.yaml` three campaigns ago does not reappear as a trend. The current
campaign's own point is always the newest, deduped by run id so `--from-store` does not
count it twice.

A change smaller than `settings.trends.dead_band_pct` (default 5%) reads as **flat**.
Emulated throttling varies run to run; without a dead band a 3% wobble is reported as a
regression every campaign and the section becomes noise the reader learns to skip.
Target crossing is reported separately from direction — a metric can improve
substantially and still be over budget, and the report should say both.

**A trend never changes a verdict.** A page that passes every threshold but got 6%
slower reports `regressed` and still passes. Folding trend into verdict would turn a
green page red without any threshold being crossed. The first campaign you ever run
reports every series as `new` and produces a complete report; a missing or unreadable
store degrades the same way, because analysis never fails over unavailable history.

**The appendix carries the evidence.** Each capture gets its screenshot embedded
as a data URI — the PDF is printed via `set_content` with no origin, so a
`file://` reference would resolve to nothing — plus the heaviest requests from
its scrubbed HAR. The table is truncated to the largest transfers and always
states the true request count and total bytes beside it, because 15 rows summing
to 2 MB reads as the whole page unless the document says the page made 214
requests totalling 8 MB.

Full-page captures run to tens of thousands of pixels tall. Past
`settings.report.appendix.screenshot_max_height_px` the image is cropped from the
top and the caption says so; scaling one to fit would produce a smear a reader
cannot distinguish from a broken capture.

A missing screenshot, a cleaned `data/raw`, or a HAR the browser truncated
degrades that entry alone — the section and both its sub-blocks always render,
and `meta.degraded_appendix_entries` counts what analysis found missing. Use
`--no-appendix-images` when the captures show an authenticated session.

`--skeleton-check` diffs that fingerprint against the committed
`report/skeleton.baseline.json` and exits non-zero on drift, naming what moved:

```
skeleton drift vs report/skeleton.baseline.json:
  - page.lcp-breakdown  (expected at index 6)
  + page.waterfall      (found at index 6)
```

It still writes the report — the rendered output is the evidence for diagnosing the
drift, and the command that spotted the problem should not withhold what you need to
understand it. Changing the template on purpose means running `--update-baseline` and
committing the one-line diff, which is the whole mechanism by which drift becomes
*visible* rather than merely detectable. A unit test renders a synthetic report and
asserts the committed baseline still matches it, so a template change with a forgotten
`--update-baseline` fails in CI rather than waiting for someone to render a real
campaign.

**Charts are inline SVG** built by pure functions from numbers. matplotlib randomises
SVG element ids per process and stamps a creation date into every file, so both are
pinned; without that, "same data, same report" is false. Because SVG is text, the tests
assert what a chart *shows* — bar count, labels, the fail-red on the failing metric.

The LCP breakdown is **derived from paint milestones**, not from the LCP entry's own
sub-part timings, which ingestion never captured. The chart says so in a visible
caption, and refuses to draw at all rather than render a negative phase.

PDFs come from Chromium print-to-PDF through an injected Playwright seam, so the whole
offline suite stays browser-free; only the real PDF run is `e2e`-marked.

---

## Testing

```bash
pytest -m "not e2e"      # 820 offline tests, no browser, no network
pytest -m e2e            # real Chromium against live pages
```

The Playwright surface and every metric collector are injected, so the offline suite
runs entirely against fakes. CI additionally enforces ≥80% coverage, runs `pip-audit`
on pinned dependencies, and runs `gitleaks` over both the working tree **and the full
commit history** — a secret committed and later removed is still a leaked secret.

CI additionally regenerates a **real** report on every push and pull request:
a headless Chromium campaign against `config/ci-targets.yaml`, analysed
`--no-llm` and rendered with `--skeleton-check`, with the resulting
`report.json`/`.html`/`.md`/`.pdf` uploaded as a build artifact. Everything
else in CI proves the parts against fakes; this is the only thing that proves
the sequence.

It gates *structure and execution*, never magnitude: the page belongs to
someone else and its numbers move for reasons that have nothing to do with this
repository. Its INP arrives as the Event Timing observer floor, so the CI
report is not a benchmark and must not be read as one. A target that does not
answer exits 3 and the job skips with a warning rather than turning a merge red
— a schema violation, a render failure or skeleton drift all still fail hard.
HAR and trace files are never uploaded (SECURITY_PLAN.md §2.6).

---

## Security

Documented in full in [SECURITY_PLAN.md](docs/SECURITY_PLAN.md). The controls that
affect day-to-day use:

- **SSRF gate.** Every URL passes `normalize.url_safety.validate_url(resolve=True)`
  _before_ any navigation, rejecting non-HTTPS, raw-IP, userinfo, and private/internal
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

| Phase | Scope                                                                | Status   |
| ----- | -------------------------------------------------------------------- | -------- |
| 0     | Foundations — config, CI, secrets hygiene                            | Done     |
| 1     | Canonical schema + manual ingestion                                  | Done     |
| 2     | Automated multi-page browser campaigns                               | Done     |
| 3     | SQLite run store + artifact persistence                              | Done     |
| 3b    | RAG — embeddings, knowledge base, retrieval, prompts                 | Done     |
| —     | Optional per-target request headers (bot-protected targets)          | Done     |
| 4     | Analysis — findings, impact, improvement estimator, Report JSON      | Done     |
| 5     | Report rendering — fixed HTML skeleton → PDF + Markdown mirror       | Done     |
| 6     | Unified CLI (`ingest` / `analyze` / `report`) + skeleton-drift check | Done     |
| 7a    | Campaign-over-campaign trends per page and condition                 | Done     |
| 7b    | Screenshot / HAR appendix embedded in the PDF                        | Done     |
| 7c    | Loopback-only web form for manual entry                              | Done     |
| 7d    | CI regeneration of a real campaign report                            | Done     |

## Documentation

- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) — full specification and design decisions
- [SECURITY_PLAN.md](docs/SECURITY_PLAN.md) — threat model and controls
- [TESTING_PLAN.md](docs/TESTING_PLAN.md) — test strategy
- [CUSTOM_HEADERS.md](docs/CUSTOM_HEADERS.md) — optional request headers for bot-protected targets
