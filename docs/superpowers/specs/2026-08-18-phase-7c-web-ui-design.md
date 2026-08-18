# Phase 7C — Local Manual-Entry Web Form

**Date:** 2026-08-18
**Status:** Approved design
**Covers:** PROJECT_SPEC.md §10 Phase 7, third bullet — new `webui/` package,
`cli.py`, `requirements.txt` (unchanged), README + PROJECT_SPEC status

---

## 1. Purpose

Manual ingestion works and has since Phase 1. It is also a twenty-five-flag
command line:

```bash
python -m cli ingest manual --page-url https://example.com/ \
  --problem "..." --lcp-ms 6200 --cls 0.42 --inp-ms 480 \
  --lh-performance 54 --total-transfer-kb 3400 --output data/processed/x.json
```

Every flag is optional, none is discoverable, and a typo in `--cls 4.2` costs
the whole invocation. The people best placed to describe a performance problem
in prose — the person who noticed it — are the least likely to assemble that
line correctly on the first try.

Phase 7C puts a form in front of the same function. It adds no capability: the
identical `Run` JSON lands in the identical place. It changes only who can
produce one.

## 2. Scope

**In:**

- `webui/form.py` — field contract, string → typed coercion, Pydantic error
  mapping. Pure; no HTTP, no I/O.
- `webui/app.py` — WSGI application. Routing, request reading, response
  building. Computes nothing.
- `webui/__main__.py` — `main(argv) -> int`; argparse, loopback guard, server.
- `webui/template/form.html.j2` + `webui/template/style.css` — the page.
- `cli.py` — one new command, `ui`.
- README + `docs/PROJECT_SPEC.md` — 7C marked done, the "Missing" table row
  removed.

**Out (deferred, with reason):**

- **Run listing.** `python -m cli list-runs` already answers "what do I have",
  and the store it reads is not the file tree this form writes to (§3.4). A
  listing in the UI would either duplicate that command or quietly disagree
  with it.
- **Triggering `analyze` / `report`.** Both are long-running; a browser button
  needs job state, progress, and cancellation — a larger sub-project than the
  form it would be bolted to.
- **Writing to the SQLite run store.** No production code path writes the
  `runs` table today (§3.4). Introducing the first one from the newest, least
  load-bearing surface is the wrong place to make that decision.
- **JavaScript.** §3.6.
- **Authentication.** §5.1 removes the thing it would protect against.

## 3. Architecture

```
browser
   │  GET /                      form, fields + constraints read from the schema
   │  POST /runs                 application/x-www-form-urlencoded
   ▼
webui/app.py  (WSGI callable)
   │
   ├─ webui/form.py     parse(form) -> kwargs        strings → float/int/None
   │                    field_errors(exc) -> {field: message}
   │
   ├─ ingest.manual.build_manual_run(**kwargs)       THE validation gate
   │        └─ normalize.url_safety.validate_url(resolve=False)
   │        └─ normalize.schema.Run.model_validate
   │
   └─ write <output-dir>/<run_id>.json  ──► 303 /?saved=<run_id>&<context>
                                            (or 400, re-rendered, values intact)
```

### 3.1 Why WSGI rather than `BaseHTTPRequestHandler`

The application is `app(environ, start_response)` — a plain callable over two
dictionaries. A test builds an `environ`, calls it, and asserts on the status
line and body. No socket, no port, no thread, no browser.

`BaseHTTPRequestHandler` puts socket I/O inside the unit under test. Testing it
means binding a real port or hand-mocking `rfile`/`wfile`, and the first is how
a fast offline suite acquires a flaky network-dependent corner. `wsgiref` is
stdlib, so this costs nothing but the choice.

`wsgiref.simple_server` is single-threaded and explicitly not a production
server. That is accurate for what this is, and §5.1 makes it true rather than
merely documented.

### 3.2 One validation gate, still

`webui/form.py` does not check a single range. It converts `"6200"` to `6200.0`
and hands the result to `build_manual_run` — the same function `ingest manual`
calls, reaching the same `Run.model_validate`. `cls` in 0..1, Lighthouse 0..100,
`lcp_ms >= 0`, the URL scheme guards: all of them arrive as `ValidationError`,
and `field_errors()` maps `exc.errors()[i]["loc"]` back to the form field that
produced it.

The rule this protects: **a validation rule must never exist in two places.**
A UI that re-implemented "CLS is 0 to 1" would be correct on the day it was
written and wrong the day the schema changed, with nothing failing in between.

The same rule governs the client-side hints. `<input type="number">` wants
`min`, `max`, and `step`, and typing them into the template creates exactly the
second copy just ruled out. Instead the template reads them from
`CwpMetrics.model_fields` / `LighthouseScores.model_fields` at render time, so
the browser's own validation is generated from the schema and cannot fall out
of step with it.

Coercion is the one thing `form.py` can get wrong on its own, so it is the one
thing unit-tested hardest:

| Input      | Result    | Why                                                  |
| ---------- | --------- | ---------------------------------------------------- |
| `""`       | `None`    | Absent, not zero — the `—` rule from the run listing |
| `"0"`      | `0.0`     | A measured zero is a measurement                     |
| `"abc"`    | field error | Pydantic would report this against a nested loc the user cannot act on |
| `" 6200 "` | `6200.0`  | Pasted values carry whitespace                       |

### 3.3 Request flow and the 303

A successful POST writes the file and answers `303 See Other` pointing at
`/?saved=<run_id>&project=…&page=…&page_url=…&device=…&network=…`.

Redirecting rather than rendering the result directly means a reload cannot
write a second run — the browser re-issues the GET, not the POST. It also
carries the context forward: the entry after this one is almost always the same
page under a different condition, so project, page, URL, device and network
come back filled while the problem text and every metric come back empty. Stale
metrics silently inherited into a new run would be a data-integrity bug that
looks like a convenience feature.

A failed POST re-renders the form at `400` with **every submitted value still
in it** and each message beside its field. Discarding a filled form over one
bad digit is the surest way to send someone back to the CLI.

### 3.4 Where the run goes, and where it does not

The file lands in `--output-dir` (default `data/processed`), named
`<run_id>.json`, with the same `run.model_dump(mode="json")` and `indent=2` the
CLI writes. `analyze` reads that directory; nothing else needs to happen for the
run to reach a report.

It is not inserted into `data/processed/runs.sqlite`. `store.sql.insert_run`
has no production caller today — `ingest/automated.py` writes run JSON and
nothing else; the only SQLite writer in the pipeline is `persist_findings`,
which writes the *vector* store. The `runs` table is read by `list-runs`,
`analyze --from-store` and `analysis/trends.py`, and populated only by tests.

That is a real pre-existing gap, and it is deliberately not closed here. The UI
matching `ingest manual` exactly is the whole point of §3.2; matching it
*except for also writing to a table the CLI does not write to* would make the
parity claim false, and would decide the question of who owns run persistence
from the newest and least load-bearing surface in the system. It is noted for
7D, which needs a real campaign in the store anyway.

`run_id` is server-generated (`run_<UTC timestamp>_<uuid4[:4]>`), so no
user-supplied string reaches a filename and no two submissions collide. Path
confinement is a property of the construction, not a check bolted after it.

### 3.5 Field set

Every field `ingest manual` accepts, grouped as the form reads rather than as
argparse declares:

| Group             | Fields                                                           |
| ----------------- | ---------------------------------------------------------------- |
| What page         | `project`, `project_url`, `page`, `page_url` *(required)*        |
| Under what        | `device`, `network`, `cpu_throttle`, `runs`                      |
| What's wrong      | `problem` (textarea), `keywords`                                 |
| Core Web Vitals   | `lcp_ms`, `cls`, `inp_ms`, `fcp_ms`, `ttfb_ms`                   |
| Targets           | `target_lcp_ms`, `target_cls`, `target_inp_ms`                   |
| Lighthouse        | `performance`, `accessibility`, `best_practices`, `seo`          |
| Network           | `total_transfer_kb`, `request_count`, `render_blocking_css`      |

`source` is fixed to `manual` and `runner` to `manual-webui`, which is what
makes a run's origin recoverable from the run itself. `--bundle-kb` is a CLI
back-compat alias for `--total-transfer-kb` and is not repeated in a UI that has
no history to be compatible with.

`device` and `network` render as `<select>` populated from `config/devices.yaml`
and `config/networks.yaml` — the same names the automated runner accepts, so a
manual run can be compared against a measured one instead of sitting in a
condition bucket of its own.

### 3.6 No JavaScript

The form submits with `method="post"`; the browser does the rest. Native
`type="number"` plus the schema-derived `min`/`max` gives client-side range
feedback for free, and the server does not trust any of it (§3.2).

The alternative is a script that duplicates validation for nicer error
placement — the second copy of the rules again, this time in a second language.
The page is one form; it does not need a runtime.

## 4. Testing

Structure follows `docs/TESTING_PLAN.md`. No new dependency, so the coverage
gate and `pip-audit` surface are unchanged.

**Unit — `tests/unit/webui_form_test.py`**

- Coercion table in §3.2, every row.
- `field_errors` maps a nested Pydantic `loc`
  (`("metrics", "cwp", "cls")`) to the form field name `cls`.
- Blank optional groups produce no keys rather than keys holding `None`.

**Integration — `tests/integration/webui_app_test.py`** (drives the WSGI
callable directly)

- `GET /` renders an input for every field in §3.5, and the `max` on `cls` is
  `1` because the schema says so — not because the test hardcoded `1`.
- Valid `POST /runs` → `303`, `Location` carries the run id, file exists,
  content parses back through `Run.model_validate`.
- **Parity:** the same inputs through `ingest.manual.main` and through the app
  produce equal payloads apart from `run_id` and `meta.created_at`
  (and `meta.runner`, per §3.5). This is the test that keeps the UI from
  becoming a second ingestion path with its own behaviour.
- `cls=1.5` → `400`, the message names `cls`, and every other submitted value
  is still present in the returned HTML.
- Missing `page_url` → `400` naming it, because it is the one required field.
- Body over 64 KB → `413`, and nothing is written.
- `Content-Type: application/json` → `415`.
- `GET /nope` → `404`; `POST /` → `405`.
- Reflected XSS: a problem description containing `<script>` comes back escaped.

**Unit — `tests/unit/webui_main_test.py`**

- `--host 0.0.0.0` returns exit 2 and the server is never constructed (asserted
  via an injected server factory, so no port is bound in the offline suite).
- `--host localhost` and `--host 127.0.0.1` are accepted.

**E2E — `tests/e2e/webui_e2e_test.py`**, `@pytest.mark.e2e`

One test: serve the app on an ephemeral port, drive it with Playwright, fill
the form, submit, assert the success banner and the written file. Every other
test proves the handler is correct; only this one proves the HTML is
submittable — an unclosed tag or a missing `name` attribute passes all of them.

## 5. Security

Reviewed against `docs/SECURITY_PLAN.md`. This phase introduces the project's
first listening socket, so the controls are stated in full.

### 5.1 Loopback only, enforced by refusal

The server binds `127.0.0.1`. `--host` accepts loopback values only; anything
else exits `2` **before the socket is created**, naming the reason.

A warning would not do. An unauthenticated endpoint that writes files to disk,
reachable from the LAN, is precisely the shape this project's threat model
exists to prevent, and a warning is a control that works only on people who
were not going to make the mistake anyway.

This is also what makes "no authentication" the right answer rather than a
missing feature: there is no remote reachability to authenticate.

### 5.2 The rest

- **Body cap.** Requests over 64 KB are refused with `413` before the body is
  read into memory. `Problem.description` is capped at 10 000 characters by the
  schema, so there is no legitimate body near that size.
- **Content type.** Only `application/x-www-form-urlencoded` is parsed.
- **No static file mapping.** `SimpleHTTPRequestHandler` is deliberately unused.
  The single stylesheet is read from a hardcoded path at startup and served from
  memory, so there is no request-path-to-filesystem-path translation and
  therefore no traversal to defend against.
- **Escaping.** Jinja autoescape is on. The form echoes user prose back into
  HTML on both the error and success paths, which is a reflected-XSS vector the
  moment it is not escaped.
- **SSRF.** Unchanged and inherited: `build_manual_run` calls
  `validate_url(resolve=False)` on both URLs. Manual ingestion never navigates,
  so DNS resolution stays skipped and the scheme, userinfo and raw-IP guards
  still hold.
- **No secrets.** The form touches no API key; the UI never reaches the RAG or
  LLM layers.

## 6. Styling

`webui/template/style.css`, standalone, sharing the report's palette tokens by
value with a comment naming `report/palette.py` as the source of truth — the
convention `report/template/style.css` already follows.

The report stylesheet is not reused. It is a print stylesheet built around
`@page`, A4 measure, and tabular figures for a document nobody types into; a
data-entry form wants labels, focus states, and error affordances it has no
reason to carry.

## 7. Consequences

- The README "Missing" table loses its first row; the roadmap marks 7C done and
  7D next.
- `python -m cli` gains `ui`, and `python -m webui` works directly like every
  other stage.
- Dependency count unchanged. `requirements.txt` is touched only if that turns
  out to be false.
