# Phase 7D — CI Regeneration of a Real Campaign Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CI job that measures a real page with a real browser, renders the real report, and fails the build when the report's skeleton drifts.

**Architecture:** `ingest auto` gains a `--targets PATH` flag so CI can run its own committed campaign (`config/ci-targets.yaml`, one page on `https://www.wikipedia.org/`, two conditions, one run each). The browser runner raises a typed `TargetUnreachableError` on navigation failure, which `ingest/automated.main` maps to exit code 3, letting the workflow treat a third party's outage as a skip while every other failure stays red. A new `live-campaign-report` job runs `ingest auto → analyze --no-llm → report --skeleton-check` on default paths and uploads the rendered document.

**Tech Stack:** Python 3.11+ (CI runs 3.13), Pydantic v2, Playwright (Chromium), pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-18-phase-7d-ci-report-regeneration-design.md`

## Global Constraints

- **Tests are offline by default.** Everything in `pytest -m "not e2e"` must run with no browser and no network. Real-browser tests carry `pytest.mark.e2e`.
- **Test file naming:** `tests/unit/<area>_test.py` (suffix `_test.py`, not `test_*.py`).
- **The SSRF gate stays in force.** No campaign target may be non-HTTPS, a raw IP, or loopback (`normalize/url_safety.py`, SECURITY_PLAN.md §2.2).
- **The skeleton baseline is evidence, not a knob.** If a real report's fingerprint disagrees with `report/skeleton.baseline.json`, fix the code that produced the disagreement. Do not run `--update-baseline` to make a job pass.
- **No secrets in CI.** The campaign runs with no API key and no bot-allowlist token; `analyze` runs `--no-llm`.
- **Exit codes owned by this plan:** `0` success, `1` failure, `2` argparse usage error (argparse's own), `3` target unreachable.
- **The live CI target is `https://www.wikipedia.org/`.** `example.com` emits no INP entry and its automated runs fail `normalize/schema.py`'s CWV-trio validator every time (measured; spec §3).
- **Commit style:** imperative subject line describing the behaviour change, body explaining why. End every commit message with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `ingest/automated.py` | `--targets` flag; exit-code mapping for an unreachable target | 1, 3 |
| `config/ci-targets.yaml` | The campaign CI runs | 2 |
| `ingest/browser/runner.py` | `TargetUnreachableError` around navigation | 3 |
| `tests/unit/browser_test.py` | Unit tests for all of the above (existing file, existing fakes) | 1, 3 |
| `tests/unit/config_test.py` | `ci-targets.yaml` loads and resolves | 2 |
| `.github/workflows/ci.yml` | The `live-campaign-report` job; delete the dead skeleton step | 5 |
| `README.md`, `docs/PROJECT_SPEC.md` | Status, roadmap, CI documentation | 6 |

Task 4 writes no files — it is the local end-to-end verification that must happen *before* the workflow is wired, and any fix it forces lands in `report/skeleton.py`.

---

### Task 1: `--targets` — point a campaign at a different targets file

**Files:**
- Modify: `ingest/automated.py` (`_build_parser`, `main`)
- Test: `tests/unit/browser_test.py` (append to the `# CLI` section, after `test_cli_config_error_returns_nonzero`)

**Interfaces:**
- Consumes: `config.load.load_config(settings=…, devices=…, networks=…, targets=…) -> ProjectConfig`, which already accepts a `targets` path and raises `ConfigError("Config file not found: <path>")` for a missing one.
- Produces: `ingest auto --targets PATH`. Task 2's config file and Task 5's workflow both depend on this flag name.

- [ ] **Step 1: Write the failing tests**

`tests/unit/browser_test.py` does not import `Path` today. Add it to the stdlib
imports at the top of the file, beside `import json`:

```python
from pathlib import Path
```

Then append to the `# CLI` section:

```python
def test_cli_targets_flag_is_forwarded_to_the_loader(monkeypatch, capsys):
    """--targets must reach load_config; nothing else selects the campaign."""
    seen = {}

    def recording_load_config(*args, **kwargs):
        seen.update(kwargs)
        return make_cfg()

    monkeypatch.setattr("config.load.load_config", recording_load_config)
    assert automated.main(["--dry-run", "--targets", "config/ci-targets.yaml"]) == 0
    assert str(seen["targets"]) == str(Path("config/ci-targets.yaml"))


def test_cli_without_targets_keeps_the_default(monkeypatch):
    """Omitting the flag must not pass a path at all, so the loader default wins."""
    seen = {}

    def recording_load_config(*args, **kwargs):
        seen["kwargs"] = kwargs
        return make_cfg()

    monkeypatch.setattr("config.load.load_config", recording_load_config)
    assert automated.main(["--dry-run"]) == 0
    assert "targets" not in seen["kwargs"]


def test_cli_missing_targets_file_exits_one_naming_the_file(capsys):
    """A bad path is a clean config error, not a traceback."""
    code = automated.main(["--dry-run", "--targets", "config/nope.yaml"])
    assert code == 1
    err = capsys.readouterr().err
    assert "nope.yaml" in err
    assert "Traceback" not in err
```

`Path` is already imported at the top of `browser_test.py`; if it is not, add `from pathlib import Path`. `make_cfg()` is the existing helper in that file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/browser_test.py -k targets -v`
Expected: FAIL — `unrecognized arguments: --targets` (argparse exits 2).

- [ ] **Step 3: Add the flag**

In `_build_parser()`, after the `--pages` argument:

```python
    p.add_argument("--targets", default=None,
                   help="Targets file to run (default: config/targets.yaml). "
                        "Lets one checkout hold more than one campaign — CI "
                        "runs config/ci-targets.yaml.")
```

In `main()`, replace the `cfg = load_config()` call:

```python
    try:
        # Passed only when given: omitting the key lets load_config's own
        # default apply, so the default campaign has exactly one definition.
        targets_kwarg = {"targets": Path(args.targets)} if args.targets else {}
        cfg = load_config(**targets_kwarg)
    except Exception as exc:  # ConfigError
        print(f"error: {exc}", file=sys.stderr)
        return 1
```

`Path` and `sys` are already imported in `ingest/automated.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/browser_test.py -k targets -v`
Expected: 3 passed.

- [ ] **Step 5: Run the whole offline suite**

Run: `pytest -m "not e2e" -q`
Expected: all pass — no existing test passes `--targets`, so the default path is unchanged.

- [ ] **Step 6: Commit**

```bash
git add ingest/automated.py tests/unit/browser_test.py
git commit -m "Let a campaign name its own targets file

CI needs to measure a page that is not the committed target, and
load_config already accepts the path — only the CLI could not reach it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The CI campaign

**Files:**
- Create: `config/ci-targets.yaml`
- Test: `tests/unit/config_test.py` (append)

**Interfaces:**
- Consumes: Task 1's `--targets` flag; `config.load.load_config`, `config.load.CONFIG_DIR`.
- Produces: `config/ci-targets.yaml` — project `ci-smoke`, page `homepage`, two conditions. Task 5's workflow passes this exact path.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/config_test.py`:

```python
def test_ci_targets_file_resolves_to_two_single_run_conditions():
    """The campaign CI runs must load and cross-validate like any other.

    Pinned deliberately: a device or network renamed in the presets would
    otherwise break CI at browser-launch time on a pull request, rather than
    in the offline suite that is meant to catch it.
    """
    from config.load import CONFIG_DIR, load_config

    cfg = load_config(targets=CONFIG_DIR / "ci-targets.yaml")

    assert cfg.project == "ci-smoke"
    assert [p.name for p in cfg.pages] == ["homepage"]
    page = cfg.pages[0]
    assert page.url.startswith("https://")
    assert [(t.device, t.network, t.runs) for t in page.tests] == [
        ("mid-mobile", "slow-4g", 1),
        ("desktop", "fast-3g", 1),
    ]
    # No bot-allowlist header: CI has no such secret and needs none.
    assert not cfg.headers


def test_ci_target_url_passes_the_ssrf_gate():
    """The CI campaign is subject to the same gate as any other target."""
    from config.load import CONFIG_DIR, load_config
    from normalize.url_safety import validate_url

    cfg = load_config(targets=CONFIG_DIR / "ci-targets.yaml")
    for page in cfg.pages:
        assert validate_url(page.url, resolve=False) == page.url
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/config_test.py -k ci_target -v`
Expected: FAIL — `ConfigError: Config file not found: …/config/ci-targets.yaml`.

- [ ] **Step 3: Write the config file**

Create `config/ci-targets.yaml`:

```yaml
# The campaign CI runs (PROJECT_SPEC §10 Phase 7D). Not the project's targets —
# `config/targets.yaml` is, and this file exists so CI never has to touch it.
#
# Why wikipedia.org and not example.com: an automated run must carry the CWV
# trio (normalize/schema.py), and INP only exists if an interaction produces an
# Event Timing entry. example.com registers no handlers, so its runs are
# rejected every time. Measured — see the design doc, §3.
#
# Why one page, two conditions, one run: the skeleton's page-count invariance is
# already proven offline (report/skeleton.py's one-page-vs-three-page test), the
# per-condition series keys are not, and the median of one renders the same
# document as the median of three. One page load per condition per CI run is all
# this asks of someone else's server.
#
# No `headers:` block: CI holds no bot-allowlist token and needs none.
project: ci-smoke
pages:
  - name: homepage
    url: https://www.wikipedia.org/
    tests:
      - { device: mid-mobile, network: slow-4g, runs: 1 }
      - { device: desktop, network: fast-3g, runs: 1 }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/config_test.py -k ci_target -v`
Expected: 2 passed.

- [ ] **Step 5: Prove the flag and the file work together**

Run: `python -m cli ingest auto --targets config/ci-targets.yaml --dry-run`
Expected, exactly:

```
homepage	mid-mobile	slow-4g	1
homepage	desktop	fast-3g	1
```

No browser launches — `--dry-run` prints the resolved matrix and returns.

- [ ] **Step 6: Commit**

```bash
git add config/ci-targets.yaml tests/unit/config_test.py
git commit -m "Add the campaign CI measures

One page, two conditions, one run each, on a page that actually emits an
INP entry — example.com has no handlers, so its automated runs fail the
CWV trio validator every time.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Unreachable is not a failure

**Files:**
- Modify: `ingest/browser/runner.py` (new exception class; wrap the `page.goto` call around line 220)
- Modify: `ingest/automated.py` (module-level constant + import, `main`'s except clauses)
- Modify: `tests/unit/browser_test.py:393-399` (`test_navigation_timeout_still_propagates`)
- Test: `tests/unit/browser_test.py` (append new cases)

**Interfaces:**
- Consumes: `ingest.browser.runner.BrowserRunner.run_condition`, `ingest.automated.main`.
- Produces:
  - `ingest.browser.runner.TargetUnreachableError(RuntimeError)` — raised from `run_condition` when navigation itself fails, message `f"Navigation to {url} failed: {original}"`, original exception preserved as `__cause__`.
  - `ingest.automated.EXIT_TARGET_UNREACHABLE = 3` — the exit code `main` returns for it. Task 5's workflow tests for literal `3`.

- [ ] **Step 1: Write the failing tests**

The file already imports runner symbols at the top; add the new one to that
block (`from ingest.browser.runner import (BlockedResponseError, BrowserRunner,
…)`) rather than importing inside each test:

```python
    TargetUnreachableError,
```

First **replace** the existing `test_navigation_timeout_still_propagates` (around `tests/unit/browser_test.py:393`) with:

```python
def test_navigation_failure_raises_target_unreachable(public_dns):
    """Unlike networkidle, failing to load at all is a real failure — and a
    *distinguishable* one, so CI can tell an outage from a broken pipeline."""
    browser = FakeBrowser(
        page_kwargs={"goto_error": FakeTimeout("Page.goto: Timeout 30000ms exceeded.")}
    )
    with pytest.raises(TargetUnreachableError) as excinfo:
        make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)

    # The original diagnosis must survive: "unreachable" without the reason
    # sends whoever reads the CI log to reproduce it by hand.
    assert "Timeout 30000ms exceeded" in str(excinfo.value)
    assert "https://example.com/" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, FakeTimeout)
```

Then append these, next to the other campaign/CLI tests:

```python
def test_blocked_response_is_not_treated_as_unreachable(public_dns):
    """A 403 is a measurement of a block page — a real result, not an outage."""
    browser = FakeBrowser(page_kwargs={"main_status": 403})
    with pytest.raises(BlockedResponseError) as excinfo:
        make_runner(browser).run_condition("https://example.com/", DEVICE, NETWORK)
    assert not isinstance(excinfo.value, TargetUnreachableError)


def test_cli_returns_three_when_the_target_is_unreachable(monkeypatch, tmp_path, capsys):
    """Exit 3, so CI can skip on someone else's outage without going red."""
    class UnreachableRunner:
        def run_condition(self, *args, **kwargs):
            raise TargetUnreachableError(
                "Navigation to https://www.wikipedia.org/ failed: net::ERR_NAME_NOT_RESOLVED"
            )

    monkeypatch.setattr("config.load.load_config", lambda *a, **k: make_cfg())
    monkeypatch.setattr(
        automated, "_real_runner", lambda cfg=None: (None, None, UnreachableRunner())
    )

    code = automated.main(["--pages", "pdp", "--output-dir", str(tmp_path)])
    assert code == automated.EXIT_TARGET_UNREACHABLE == 3
    assert "unreachable" in capsys.readouterr().err.lower()


def test_cli_still_returns_one_for_every_other_failure(monkeypatch, tmp_path, capsys):
    """Only navigation is environmental; a broken pipeline must stay red."""
    class BrokenRunner:
        def run_condition(self, *args, **kwargs):
            raise RuntimeError("collector exploded")

    monkeypatch.setattr("config.load.load_config", lambda *a, **k: make_cfg())
    monkeypatch.setattr(
        automated, "_real_runner", lambda cfg=None: (None, None, BrokenRunner())
    )

    code = automated.main(["--pages", "pdp", "--output-dir", str(tmp_path)])
    assert code == 1
    assert "collector exploded" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/browser_test.py -k "unreachable or navigation_failure or every_other_failure" -v`
Expected: FAIL — `ImportError: cannot import name 'TargetUnreachableError'` and `AttributeError: module 'ingest.automated' has no attribute 'EXIT_TARGET_UNREACHABLE'`.

- [ ] **Step 3: Add the exception and raise it**

In `ingest/browser/runner.py`, after the `BlockedResponseError` class:

```python
class TargetUnreachableError(RuntimeError):
    """Navigation itself failed — DNS, TLS, connection refused, or timeout.

    Separated from every other failure so a caller can distinguish "the
    network did not cooperate" from "this pipeline is broken". CI treats the
    first as a skip and the second as a red build (PROJECT_SPEC §10 Phase 7D);
    matching on Playwright's error text in a shell script could not be tested.

    Deliberately *not* raised for a non-2xx document: that is a real
    measurement of a block page, and `BlockedResponseError` already says so.
    """
```

Then wrap the navigation call (currently `ingest/browser/runner.py:220-222`):

```python
            try:
                response = page.goto(
                    url, wait_until="load", timeout=self._navigation_timeout_ms
                )
            except Exception as exc:
                # The original message is the diagnosis — a bare "unreachable"
                # sends whoever reads the CI log to reproduce it by hand.
                raise TargetUnreachableError(
                    f"Navigation to {url} failed: {exc}"
                ) from exc
```

Leave the `main_status` check and its `BlockedResponseError` outside the `try`, exactly where they are — a block page is not an unreachable target.

- [ ] **Step 4: Map it to an exit code**

In `ingest/automated.py`, at module level next to `DEFAULT_RUNNER_NAME`:

```python
from ingest.browser.runner import TargetUnreachableError

#: Exit code for "the target did not answer", as distinct from a failure of
#: this pipeline. 3 rather than 2, which argparse owns for usage errors.
EXIT_TARGET_UNREACHABLE = 3
```

The import is safe at module level: `ingest/browser/runner.py` imports no Playwright package at import time (only `config.load`, `normalize.url_safety` and the stdlib), so `list-runs` and the offline suite pay nothing for it.

In `main()`, add a clause **before** the existing `except Exception`:

```python
    except TargetUnreachableError as exc:
        print(f"error: target unreachable: {exc}", file=sys.stderr)
        return EXIT_TARGET_UNREACHABLE
    except Exception as exc:
        print(f"error: campaign failed: {exc}", file=sys.stderr)
        return 1
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit/browser_test.py -v`
Expected: all pass, including the untouched `test_context_is_closed_even_when_navigation_fails` — it matches on `RuntimeError` and the text `nav boom`, both of which the new message preserves.

- [ ] **Step 6: Run the whole offline suite**

Run: `pytest -m "not e2e" -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add ingest/browser/runner.py ingest/automated.py tests/unit/browser_test.py
git commit -m "Tell an unreachable target apart from a broken pipeline

CI runs the campaign against a page it does not own, so a third party's
outage must not turn a merge red — while everything else still must.
A typed error and exit code 3 make that distinction testable; matching
Playwright's error text in a shell script would not have been.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Prove the real pipeline against the committed skeleton

This is the task the spec's §5 open risk lives in. `report/skeleton.baseline.json` was generated from a *synthetic* render, and nothing has ever compared it against a report built from real captures. Find out before CI does.

**Files:**
- Modify (only if the verification demands it): `report/skeleton.py`
- Test: existing `tests/unit/skeleton_test.py` (only if a fix lands)

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: evidence. No new public interface.

- [ ] **Step 1: Run the real campaign**

Needs network and Chromium (`python -m playwright install chromium` if absent).

Run: `python -m cli ingest auto --targets config/ci-targets.yaml`
Expected: two JSON files under `data/processed/`, artifacts under `data/raw/homepage/`, exit 0. If it exits 3, the network is down — retry; do not proceed on a skip.

- [ ] **Step 2: Analyze**

Run: `python -m cli analyze --no-llm`
Expected: prints `data/reports/<campaign-id>/report.json`, then `1 page(s), verdict=…, mode=rule_based`.

- [ ] **Step 3: Render and check the skeleton — the moment of truth**

Run: `python -m cli report --skeleton-check`
Expected: `report.html`, `report.md`, `report.pdf` written beside the JSON, and `skeleton ok: N sections match report/skeleton.baseline.json`, exit 0.

- [ ] **Step 4: If — and only if — it drifted, diagnose it**

The command prints what moved:

```
skeleton drift vs report/skeleton.baseline.json:
  - page.lcp-breakdown  (expected at index 6)
  + page.waterfall      (found at index 6)
```

A real campaign and a synthetic one must produce the same fingerprint; the fingerprint collapses repeating blocks precisely so that per-page and per-capture repetition does not change it. So drift here means `report/skeleton.py` fails to collapse something a real report repeats and a synthetic one does not — most likely appendix entries, one per capture.

Fix it in `report/skeleton.py`, and add the case to `tests/unit/skeleton_test.py` as a synthetic report with **two** appendix entries whose fingerprint must equal the one-entry report's. **Do not run `--update-baseline`.** A baseline rewritten to match whatever came out can never detect anything again.

- [ ] **Step 5: Confirm the appendix embedded real captures**

Run: `python -c "import json,glob; p=sorted(glob.glob('data/reports/*/report.json'))[-1]; d=json.load(open(p)); print(p); print('degraded:', d['meta'].get('degraded_appendix_entries')); print('entries:', len(d['appendix']['entries']))"`
Expected: `degraded: 0` and one entry per capture. A non-zero count means the screenshots did not resolve under `settings.storage.raw_dir` — which is exactly why CI uses the default paths rather than a scratch directory.

- [ ] **Step 6: Open the PDF**

Confirm by eye that `data/reports/<campaign-id>/report.pdf` has the screenshot in the appendix and real numbers for wikipedia.org. This is the artifact CI will upload; look at it once by hand.

- [ ] **Step 7: Commit only if a fix landed**

If Steps 1–6 passed clean, there is nothing to commit — record in the task notes that the real fingerprint matched the committed baseline. Otherwise:

```bash
git add report/skeleton.py tests/unit/skeleton_test.py
git commit -m "Collapse repeating appendix blocks in the skeleton fingerprint

A real campaign's report carries one appendix entry per capture; the
baseline was generated from a synthetic render with one. The fingerprint
is meant to be invariant to that repetition and was not.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: The CI job

**Files:**
- Modify: `.github/workflows/ci.yml` (delete lines 65-67; append the new job)

**Interfaces:**
- Consumes: `--targets` (Task 1), `config/ci-targets.yaml` (Task 2), exit code 3 (Task 3), the verification of Task 4.
- Produces: a `live-campaign-report` job and a `campaign-report` build artifact.

- [ ] **Step 1: Delete the dead step**

Remove from the `security-and-tests` job (`.github/workflows/ci.yml:65-67`):

```yaml
      - name: Determinism / skeleton check
        run: |
          python -m src.cli --skeleton-check || echo "skeleton-check not yet wired (Phase 6)"
```

There is no `src` package, `cli` requires a command token, and the `||` swallows the failure — the step has reported green since Phase 6 without ever checking anything. Its data-free half is already a unit test in `tests/unit/skeleton_test.py`; its real half is the job below.

- [ ] **Step 2: Add the job**

Append to `.github/workflows/ci.yml`, at the same indentation as `security-and-tests:`:

```yaml
  live-campaign-report:
    # Regenerates a REAL report: real Chromium, real network, real page, then
    # holds the rendered document against the committed skeleton baseline.
    # Everything else in CI proves the parts; this proves the sequence.
    runs-on: ubuntu-latest
    needs: security-and-tests   # no browser minutes while the suite is red
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          python -m playwright install --with-deps chromium

      - name: Campaign against the live CI target
        id: campaign
        run: |
          set +e
          python -m cli ingest auto --targets config/ci-targets.yaml
          code=$?
          set -e
          # 3 = the target did not answer (ingest/automated.py). The page
          # belongs to someone else; their outage is not this repo's failure.
          if [ "$code" -eq 3 ]; then
            echo "::warning::CI target unreachable — report regeneration skipped"
            echo "skipped=true" >> "$GITHUB_OUTPUT"
            exit 0
          fi
          exit $code

      - name: Analyze the campaign
        if: steps.campaign.outputs.skipped != 'true'
        # --no-llm: no API key in CI, no quota spent, no dependency on a model
        # being up. The skeleton is identical either way — that is the point.
        run: python -m cli analyze --no-llm

      - name: Render the report and check the skeleton
        if: steps.campaign.outputs.skipped != 'true'
        run: python -m cli report --skeleton-check

      - name: Upload the regenerated report
        # always(): a drifted report is exactly the artifact needed to diagnose
        # the drift, and --skeleton-check writes it before exiting non-zero.
        # HAR and trace files under data/raw are NOT uploaded — SECURITY_PLAN
        # §2.6 treats them as sensitive and a build artifact is downloadable.
        if: always() && steps.campaign.outputs.skipped != 'true'
        uses: actions/upload-artifact@v4
        with:
          name: campaign-report
          path: |
            data/reports/**/report.json
            data/reports/**/report.html
            data/reports/**/report.md
            data/reports/**/report.pdf
          if-no-files-found: error
```

- [ ] **Step 3: Validate the YAML parses**

Run: `python -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/ci.yml')); print(sorted(d['jobs'])); print(d['jobs']['live-campaign-report']['needs'])"`
Expected: `['live-campaign-report', 'security-and-tests']` then `security-and-tests`.

- [ ] **Step 4: Confirm the dead step is gone**

Run: `grep -n "src\.cli" .github/workflows/ci.yml; echo "exit=$?"`
Expected: no matching lines, then `exit=1` — grep exits 1 when it finds nothing.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "Regenerate a real campaign report in CI

The old skeleton-check step invoked a package that does not exist and
swallowed the failure with ||, so it has reported green since Phase 6.
Replaced with a job that measures a real page, renders the document, and
diffs it against the committed baseline. An unreachable target skips;
everything else is red.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md` (status paragraph, gap table, Testing section, roadmap, test count)
- Modify: `docs/PROJECT_SPEC.md:545`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing code depends on.

- [ ] **Step 1: Get the real test count**

Run: `pytest -m "not e2e" -q 2>&1 | tail -3`
Note the number; the README currently claims 812 and must state the true figure.

- [ ] **Step 2: Update the README status section**

Add to the "Working today" list, before the closing text: `· **a CI job that regenerates a real campaign report and gates its skeleton**`.

Replace the whole "Missing" block:

```markdown
**Missing:** nothing — every phase in the [Roadmap](#roadmap) is built. Anything
this README does not describe as working is not there.
```

- [ ] **Step 3: Document the job under Testing**

Append to the **Testing** section:

```markdown
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
```

- [ ] **Step 4: Update the roadmap and test count**

In the roadmap table set `7d` — `CI regeneration of a real campaign report` — to `Done`, and remove the `**Next**` marker. Update the test count in the Testing code block to the number from Step 1.

- [ ] **Step 5: Update PROJECT_SPEC**

Replace `docs/PROJECT_SPEC.md:545-547` with:

```markdown
- [x] **7d — CI regeneration of a real campaign report.** `config/ci-targets.yaml`
      plus the `--targets` flag give CI its own campaign; `TargetUnreachableError`
      and exit code 3 let a third party's outage skip rather than fail the build.
      The `live-campaign-report` job runs the real sequence and diffs the
      rendered document against `report/skeleton.baseline.json`. Design:
      `docs/superpowers/specs/2026-08-18-phase-7d-ci-report-regeneration-design.md`.
```

- [ ] **Step 6: Verify no stale claim survives**

Run: `grep -n "Missing\|Next\|src.cli\|812" README.md docs/PROJECT_SPEC.md`
Expected: no line claims a missing feature, no `**Next**` row, no reference to `src.cli`, and no stale test count.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/PROJECT_SPEC.md
git commit -m "Say that the pipeline is regenerated in CI

The gap table has no rows left. The new paragraph is explicit that the
job gates structure and execution rather than performance: its target is
someone else's page and its INP is an observer floor.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Done when

- `pytest -m "not e2e"` passes, with the new cases in `tests/unit/browser_test.py` and `tests/unit/config_test.py`.
- `python -m cli ingest auto --targets config/ci-targets.yaml --dry-run` prints the two conditions.
- A real local run has produced a `report.pdf` and `skeleton ok: N sections match report/skeleton.baseline.json` (Task 4), with `report/skeleton.baseline.json` unmodified.
- `.github/workflows/ci.yml` contains `live-campaign-report` and no reference to `src.cli`.
- README's gap table is empty, its roadmap has no `**Next**` row, and its test count is current.
