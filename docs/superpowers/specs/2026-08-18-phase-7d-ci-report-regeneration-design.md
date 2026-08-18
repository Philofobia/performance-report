# Phase 7D — CI Regeneration of a Real Campaign Report

**Date:** 2026-08-18
**Status:** Approved design
**Covers:** PROJECT_SPEC.md §10 Phase 7, fourth bullet — `.github/workflows/ci.yml`,
`ingest/automated.py`, `ingest/browser/runner.py`, new `config/ci-targets.yaml`,
README + PROJECT_SPEC status

---

## 1. Purpose

The pipeline is end to end, and nothing in CI runs it end to end.

CI today proves the parts: unit and component tests against fakes, e2e browser
tests against an intercepted fixture, and one unit test that renders a
*synthetic* Report JSON and asserts the committed
`report/skeleton.baseline.json` still matches it. Every one of those can pass
while the actual sequence — browser campaign → normalized runs → analysis →
rendered report — is broken, because no job has ever executed that sequence.

The last step of the existing job admits as much:

```yaml
- name: Determinism / skeleton check
  run: |
    python -m src.cli --skeleton-check || echo "skeleton-check not yet wired (Phase 6)"
```

There is no `src` package in this repo, and `cli` requires a command token, so
the command fails, the `||` swallows it, and the step has reported green since
Phase 6 shipped. A check that cannot fail is worse than no check: it occupies
the slot where a real one would be noticed as missing.

Phase 7D replaces it with a job that measures a real page with a real browser
and renders the real document, then holds that document against the committed
skeleton.

## 2. Scope

**In:**

- `config/ci-targets.yaml` — the campaign CI runs: one page, two conditions,
  one run each.
- `ingest/automated.py` — a `--targets PATH` option, and a distinct exit code
  for an unreachable target.
- `ingest/browser/runner.py` — `TargetUnreachableError`, raised when navigation
  itself fails.
- `.github/workflows/ci.yml` — a `live-campaign-report` job; the dead
  skeleton-check step deleted.
- README + `docs/PROJECT_SPEC.md` — 7D marked done, the "Missing" table emptied.

**Out (deferred, with reason):**

- **Committing the generated report.** A report whose numbers come from a live
  third-party page changes on every run; committing it would produce a diff per
  CI run that no one reads and that says nothing about the code. The report
  uploads as a build artifact instead — retrievable for the run that produced
  it, tied to nothing.
- **Trend history across CI runs.** Trends need a persisted store carried
  between builds. A cached SQLite file would make one build's report depend on
  another build's leftovers, which is the opposite of what a regression gate
  should be. Every CI campaign reports its series as `new`, which is correct
  for a campaign with no history.
- **Failing on a performance regression.** The measured page belongs to someone
  else and its numbers move for reasons that have nothing to do with this
  repository. The job gates *structure and execution*, never magnitude.
- **LLM analysis.** §4.2.

## 3. What "a real campaign" means here

The campaign is real: real DNS, real Chromium, real network throttling, a real
page over the wire. Two constraints shape which page.

**The SSRF gate forbids serving our own.** `normalize/url_safety.validate_url`
rejects non-HTTPS, raw-IP and loopback targets, so a fixture site served from
`127.0.0.1` inside the runner cannot be a campaign target without weakening a
control SECURITY_PLAN.md §2.2 documents. It stays in force; the target is
external.

**The CWV trio gate forbids a trivial page.** `normalize/schema.py`'s
`_require_cwv_for_automated` rejects an automated run missing LCP, CLS or INP,
and `ingest/browser/webser.py` emits no INP entry for a page with no
interaction handlers. Measured directly, mid-mobile / slow-4g:

| page | lcp_ms | cls | inp_ms | outcome |
|---|---|---|---|---|
| `example.com` | 3246.8 | 0.0 | **None** | fails validation, every time |
| `www.wikipedia.org` | 1796.9 | 0.071 | 16.0 | validates |
| `developer.mozilla.org` | 866.1 | 0.047 | 16.0 | validates |

`example.com` — the obvious choice, and the one the e2e suite already uses for
its live-site probe — is a bare document with no handlers. It cannot produce a
valid automated run. **The target is `https://www.wikipedia.org/`:** light,
stable, non-commercial, no bot filter, no login, and one throttled page load
per condition per CI run.

Its INP arrives as `16.0` — the Event Timing observer floor, not a real
interaction latency. The job proves the pipeline runs and the skeleton holds.
It is not a performance benchmark, and §6 says so in the README.

## 4. Architecture

```
config/ci-targets.yaml
        │
        ▼
python -m cli ingest auto --targets …        real Chromium, real network
        │   data/processed/*.json            normalized Runs (schema-gated)
        │   data/raw/…                       HAR + trace + screenshot
        ▼
python -m cli analyze --no-llm               rule-based; no API key, no quota
        │   data/reports/<campaign>/report.json
        ▼
python -m cli report --skeleton-check        HTML + MD + PDF, fingerprint diffed
        │
        ▼
actions/upload-artifact                      the rendered document
```

Every path is the **default**, and no step overrides one. Two reasons. The
appendix resolves screenshots under `settings.storage.raw_dir` and refuses
anything outside it, so a campaign writing to a scratch directory would render
every capture as a path-only row and count it in `meta.degraded_appendix_entries`
— the job would pass while silently exercising the degraded path. And
`data/processed`, `data/raw` and `data/reports` are all gitignored, so a fresh
checkout contains none of them and nothing stale can leak into the campaign.
The commands CI runs are therefore the commands the README documents, with one
flag added.

### 4.1 The campaign

```yaml
# config/ci-targets.yaml
project: ci-smoke
pages:
  - name: homepage
    url: https://www.wikipedia.org/
    tests:
      - { device: mid-mobile, network: slow-4g, runs: 1 }
      - { device: desktop, network: fast-3g, runs: 1 }
```

One page, because the skeleton's page-count invariance is already proven
offline by the one-page-vs-three-page fingerprint test; a second page here would
double the traffic to a third party to re-prove it. Two conditions, because the
per-condition series keys are a structure the renderer builds and a one-condition
campaign would never exercise. `runs: 1`, because the median of one is the same
document as the median of three and this job measures nothing that needs the
noise reduction.

No `headers:` block: the committed `config/targets.yaml` carries an
`X-Akamai-Bot: ${AKAMAI_BOT_TOKEN}` reference for a bot-protected target, and
CI has no such secret and needs none.

### 4.2 `--no-llm` is not a degradation here

`analysis/__main__.py` builds the embedding and LLM clients only when `--no-llm`
is absent, so the flag means no API key in CI, no quota consumed, and no
network dependency on Google. The report it produces is the documented
rule-based path with `meta.analysis_mode` stating so. The skeleton is identical
either way — that is the property the whole report layer is built around, and
this job is one more place it is asserted.

### 4.3 Pointing the campaign somewhere else: `--targets`

`ingest/automated.main` calls `load_config()` with no arguments, so the campaign
is always the committed `config/targets.yaml`. `load_config` already accepts a
`targets` path; only the CLI cannot reach it.

```
--targets PATH    Targets file to run (default: config/targets.yaml)
```

Forwarded to `load_config(targets=Path(args.targets))`. A missing file surfaces
through the existing `ConfigError` handling — `error: …`, exit 1 — with no new
error path.

The alternatives were an environment variable read inside the loader, which
makes the config source invisible at every call site, and a CI step that copies
its targets over the tracked file, which mutates a tracked config and leaves the
campaign reproducible from no committed command. A flag is the only one of the
three that a person can also use.

### 4.4 Unreachable is not a failure: `TargetUnreachableError` and exit 3

The job runs on every push and pull request, so a third party's outage must not
turn a merge red. That requires CI to distinguish "the network did not
cooperate" from "this repository is broken" — and to do it without matching on
Playwright's error text in a shell script, which nothing could test.

`ingest/browser/runner.py` wraps its `page.goto` call and raises

```python
class TargetUnreachableError(RuntimeError):
    """Navigation itself failed — DNS, TLS, connection refused, or timeout."""
```

preserving the original message. `ingest/automated.main` maps that one exception
to **exit code 3**; every other exception keeps exit 1.

| code | meaning | CI |
|---|---|---|
| 0 | campaign completed | continue to analyze |
| 1 | campaign failed | **red** |
| 2 | argparse usage error | **red** |
| 3 | target unreachable | `::warning::`, job ends green |

Three, not two, because argparse owns 2 for usage errors and a caller cannot
tell the two apart otherwise.

The scope is deliberately narrow. `BlockedResponseError` — a non-2xx main
document — stays exit 1: a bot filter answering `403` is a measurement of a
block page, which is a real result the pipeline must keep refusing. Only
navigation failing outright is environmental.

### 4.5 The job

`live-campaign-report`, `needs: security-and-tests`: no browser minutes are
spent when the suite is already failing. It checks out, installs, installs
Chromium, runs the three commands of §4, and uploads
`data/reports/**/report.{json,html,md,pdf}` with `if: always()` — a drifted
report is exactly the artifact you need to diagnose the drift, and
`--skeleton-check` writes it before exiting non-zero for that reason.

The HAR and trace files under `data/raw` are **not** uploaded. SECURITY_PLAN.md
§2.6 treats them as sensitive, and a build artifact is downloadable by anyone
who can read the run.

The existing `Determinism / skeleton check` step is deleted rather than fixed.
Its data-free half already runs as a unit test, and its real half is this job.

## 5. What can fail, and what that means

| Failure | Exit | Verdict |
|---|---|---|
| Wikipedia unreachable / navigation timeout | 3 | skip, green, annotated |
| Run missing a CWV metric | 1 | red — the collectors regressed |
| Analysis raises | 1 | red |
| Render raises | 1 | red |
| Skeleton fingerprint drifted | non-zero from `report` | red — the report changed shape |

**The open risk.** `report/skeleton.baseline.json` was generated from a
synthetic render. Whether a real campaign's document — real captures, real HAR
rows in the appendix — collapses to the identical fingerprint is unverified,
because until this job exists nothing has ever compared the two.

The pipeline therefore runs locally end to end before the workflow is wired. If
the real fingerprint differs, that is a defect in how `report/skeleton.py`
collapses repeating blocks, and it is fixed there. The baseline is **not**
regenerated to make the job pass: a baseline updated to match whatever came out
is a baseline that can never detect anything.

## 6. Testing

Offline (`pytest -m "not e2e"`, no browser, no network):

- `--targets` reaches `load_config` with the given path; its absence keeps the
  default.
- A `--targets` path that does not exist exits 1 with a message naming the file.
- `config/ci-targets.yaml` loads, resolves its device and network names, and
  yields exactly two conditions of one run each.
- A runner raising `TargetUnreachableError` makes `main` return 3; a runner
  raising anything else still returns 1; a successful campaign still returns 0.
- Navigation failure inside `run_condition` raises `TargetUnreachableError` with
  the original message preserved (fake page whose `goto` raises).

Live, once, as evidence for §5's open risk: the three commands against
`config/ci-targets.yaml`, producing a real `report.pdf` and a passing
`--skeleton-check`.

## 7. Documentation

- README: the "Missing — the rest of phase 7" table has no rows left; the
  section states the pipeline is regenerated end to end in CI. A paragraph under
  **Testing** describes the job, what it gates, and — explicitly — that its
  numbers are not a benchmark and its INP is an observer floor. Roadmap `7d` →
  `Done`. Test count refreshed.
- `docs/PROJECT_SPEC.md` §10: 7d checked, with the design's path.
