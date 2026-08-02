# Design — README, PR #3 landing, and optional custom request headers

> **Created:** 2026-08-02 · **Owner:** m.parisi
> **Scope:** Three sequential deliverables: (1) author the repository README,
> (2) land PR #3 so the README reaches `main`, (3) add *optional* per-target
> HTTP request headers so bot-protected sites (Akamai allowlist) can be measured.

---

## 1. Context

`performance-report` is a Python performance-analysis platform. Phases 0–3b are
implemented (config, canonical schema, manual ingestion, CDP-direct browser
campaigns, SQLite storage, RAG over a curated knowledge base). Phases 4–7
(analysis, report rendering, CLI orchestration) are not yet built.

The repository has no README. PR #3 (`feat/phase-3b-rag` → `main`) is open and
carries the RAG layer.

Separately, target sites such as `oakley.com` sit behind Akamai bot protection.
An issued allowlist token, sent as a request header, marks automated traffic as
authorized. Without it, requests return `403`/`429` and any measurement taken is
of a block page rather than the site.

---

## 2. Deliverable 1 — `README.md`

A root `README.md` describing the system as it actually exists today.

**Sections:** what the system is · phase status (0–3b done, 4–7 pending) ·
condensed architecture · quickstart (venv → `requirements.txt` →
`playwright install chromium` → `.env`) · configuring the test matrix
(`targets.yaml` / `devices.yaml` / `networks.yaml`) · manual and automated
ingestion usage · the RAG layer and knowledge base · testing · security posture ·
roadmap · links into `docs/`.

**Accuracy constraints.** The README must reflect implemented reality, not the
original spec's intentions:

- Vector storage is **SQLite BLOBs + numpy exact cosine search**, not ChromaDB
  (PROJECT_SPEC §8.1).
- The LLM/embedding client is **`google-genai`**, not LiteLLM.
- Main-thread metrics come **directly from CDP**; Lighthouse is opt-in via an
  injected `run_lighthouse_fn`, not required.
- Test suite: **311 offline tests** (`pytest -m "not e2e"`), plus 10 e2e tests
  behind the `e2e` marker. CI enforces ≥80% coverage.
- Phases 4–7 must be presented as roadmap, never as working features.

---

## 3. Deliverable 2 — Land PR #3

The `gh` CLI is not installed on this machine, so the PR is landed with git
directly.

1. Commit `README.md` to `feat/phase-3b-rag`; push.
2. `git checkout main`
3. `git merge --no-ff feat/phase-3b-rag -m "Merge pull request #3 from Philofobia/feat/phase-3b-rag"`
4. Push `main`. GitHub matches the branch head and auto-closes PR #3 as
   **merged**, consistent with how PRs #1 and #2 landed.

**Confirmation gate:** pushing to `main` is outward-facing and hard to reverse.
Confirm with the user immediately before step 4.

The untracked `.agents/skills/*` directories are out of scope and stay
uncommitted.

---

## 4. Deliverable 3 — Optional custom request headers

### 4.1 The governing requirement: optionality

**Header support must be completely opt-in.** A configuration with no headers
must produce behaviour byte-identical to today's. This is a hard requirement,
not a preference — it governs every decision below.

Concretely:

- When no headers resolve for a page, `extra_http_headers` is **absent** from
  the `new_context` kwargs. Not an empty dict — the key is not added at all.
- Headers are declared in `config/targets.yaml`, so the choice is made *when
  configuring, before a run starts*.
- A page may declare `headers: {}` to opt out of project-wide headers.
- `--no-headers` on the campaign CLI discards all configured headers for that
  invocation, enabling with/without comparisons without editing YAML.

### 4.2 Configuration shape

Header **names** are committed (they document which sites need what); header
**values** are `${ENV_VAR}` references resolved at load time from the
environment, after `load_dotenv()`. Secrets never enter git.

```yaml
# config/targets.yaml
project: oakley
headers:                              # project-wide, optional
  X-Akamai-Bot: ${AKAMAI_BOT_TOKEN}
pages:
  - name: homepage
    url: https://www.oakley.com/en-us
  - name: plp
    url: https://www.oakley.com/en-us/category/sunglasses
    headers: {}                       # opt out of the project header
```

```env
# .env — gitignored
AKAMAI_BOT_TOKEN=<real token>
```

Effective headers for a page = project headers, with page headers merged over
them (page wins per key). A page declaring `headers: {}` explicitly resolves to
no headers.

### 4.3 Component changes

**`config/load.py`**
- `headers: Dict[str, str]` field on `TargetsConfig` and `PageTarget`, both
  defaulting to empty.
- A resolver expanding `${VAR}` from the environment.
- On an unset variable: raise `ConfigError` naming the **header and the variable**
  — never the value.
- Reject CR/LF in header names and values (request-header injection), and
  reject empty names.
- `ProjectConfig.headers_for(page) -> Dict[str, str]` performs the merge.

**`ingest/browser/runner.py`**
- `run_condition(..., extra_http_headers: Optional[Dict[str, str]] = None)`.
- When truthy, set `ctx_kwargs["extra_http_headers"]` *before*
  `new_context(**ctx_kwargs)`. Context level is deliberate: Playwright applies
  context headers to every request from every page in the context, covering the
  main document **and all sub-resources** (scripts, images, XHR/fetch). A
  per-navigation header would leave sub-resources exposed to blocking.
- Attach a `response` listener recording `main_status` (the document response)
  and counting `403`/`429` responses as `blocked_requests`.

**`ingest/automated.py`**
- `run_campaign` resolves each page's headers via `headers_for` and passes them
  through; `--no-headers` suppresses them.
- Propagate `main_status` / `blocked_requests`.

**Artifact hygiene**
- Strip configured header names plus `Cookie` and `Authorization` from recorded
  HAR (SECURITY_PLAN §2.6). HARs are gitignored, but the token must not sit on
  disk in plaintext.

**`.env.example`** — add `AKAMAI_BOT_TOKEN=` with a comment that it is optional.

**`config/targets.yaml`** — retain the `example.com` sample as the active
default; add a commented, fully-worked Oakley block to uncomment.

### 4.4 Block handling

Blocked-request data is **always recorded**, never inferred.

- **Fail the run** when the main document status is non-2xx. This is
  unambiguous: the measurement is of a block/error page, and storing its CWV
  values would poison both the report and accumulated RAG findings.
- **Warn only** on sub-resource `403`/`429`. A stray third-party block should not
  invalidate an otherwise-valid measurement, and running deliberately without a
  token is a supported workflow.

This rule is independent of whether headers are configured, so it behaves
correctly in both modes.

### 4.5 Verification signals

Two signals confirm a token was accepted, mirroring the source how-to:

1. `main_status == 200` (a block returns `403`/`429`).
2. `blocked_requests == 0` across all sub-resources.

### 4.6 Tests (all offline, no real browser)

- Header resolution: `${VAR}` expansion; missing variable raises `ConfigError`
  naming header and variable but not the value; page-over-project merge;
  `headers: {}` opt-out; CR/LF and empty-name rejection.
- Runner: headers present → `extra_http_headers` in `new_context` kwargs;
  **headers absent → the key is not present at all** (the optionality guarantee).
- Block accounting: `main_status` capture, `blocked_requests` counting,
  non-2xx main document fails, sub-resource blocks warn only.
- HAR scrubbing removes configured header names, `Cookie`, `Authorization`.

### 4.7 Documentation

`docs/AKAMAI_HEADERS.md`, adapted from the source how-to to this pipeline:
why headers belong at context level, how to configure and how to turn them off,
the two verification signals, and the measurement guidance (run each URL 5+
times and take the median; TTFB especially varies with Akamai edge cache state,
with roughly 20× swings observed between cold and warm).

---

## 5. Sequencing

Deliverables 1 and 2 land first, on the existing `feat/phase-3b-rag` branch.
Deliverable 3 follows on a new branch cut from the updated `main`, keeping PR #3
unchanged in scope.

---

## 6. Out of scope

- Reproducing the source how-to's standalone Node script. The Python pipeline is
  the single measurement path so results feed the SQLite store, RAG, and the
  report skeleton.
- Phases 4–7 work.
- Committing the `.agents/skills/*` directories.
