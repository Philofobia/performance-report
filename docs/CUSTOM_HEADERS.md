# Custom request headers (bot-allowlist tokens)

Some targets sit behind bot protection — Akamai, for example — which flags automated
traffic and answers with `403`/`429`. When the site owner issues an allowlist token,
sending it as a request header marks the traffic as a known, authorized bot and the
campaign measures the real site instead of a block page.

This is **entirely optional**. Configure no headers and every run behaves exactly as
it did before the feature existed — the browser context is constructed identically,
and no environment variable is read.

---

## Configure it

Header **names** go in `config/targets.yaml` (committed — they document which site
needs what). Header **values** are `${ENV_VAR}` references resolved at run time, so
the secret itself never enters git.

```yaml
# config/targets.yaml
project: oakley
headers:                                # project-wide
  X-Akamai-Bot: ${AKAMAI_BOT_TOKEN}
pages:
  - name: homepage
    url: https://www.oakley.com/en-us
  - name: plp
    url: https://www.oakley.com/en-us/category/sunglasses
  - name: pdp
    url: https://www.oakley.com/en-us/product/W0OO9102?variant=888392335937
```

```env
# .env — gitignored, never committed
AKAMAI_BOT_TOKEN=<the issued token>
```

Then run as usual; nothing else changes:

```bash
python -m ingest.automated --pages homepage,plp,pdp
```

### Scoping

| Declaration | Effect |
|---|---|
| `headers:` at project level | Applied to every page |
| `headers:` on a page | Merged over the project's, key by key |
| `headers: {}` on a page | That page sends **none** |
| No `headers:` anywhere | Nothing is added, nothing is read |
| `--no-headers` on the CLI | All configured headers ignored for that run |

`--no-headers` exists so you can measure the same targets with and without the token
and compare, without editing config. With it, an unset token is not an error.

---

## Why the header is set at the context level

Playwright applies `extra_http_headers` set on a **browser context** to every request
made by every page in that context. That is the robust place for the token: it covers
the main HTML document *and* every sub-resource — scripts, images, XHR/fetch.

If the header were attached per navigation instead, only the document would carry it
and the bot filter could still block individual sub-resource requests. On a page
issuing 180–250 requests, that difference is the difference between zero blocks and a
partial, misleading measurement.

The runner does this in `ingest/browser/runner.py`, adding the key to the context
kwargs only when headers were actually supplied:

```python
if extra_http_headers:
    ctx_kwargs["extra_http_headers"] = dict(extra_http_headers)
context = self._browser.new_context(**ctx_kwargs)
```

---

## Confirming it worked

Every run records two signals under `guard`:

| Signal | Accepted | Rejected |
|---|---|---|
| `main_status` | `200` | `403` / `429` |
| `blocked_requests` | `0` | one or more |

Their handling differs deliberately:

- **A non-2xx main document fails the run** (`BlockedResponseError`). A block page
  produces real, fast Core Web Vitals numbers; storing them would silently poison the
  report and the accumulated RAG findings. Better to stop loudly.
- **Sub-resource `403`/`429` are counted and reported, but do not fail the run.** A
  stray third-party block should not invalidate an otherwise-valid measurement, and
  running deliberately without a token is a supported workflow.

If the token is wrong or missing, the main status becomes `403` and the page title is
an Akamai block page rather than the real one.

---

## Getting trustworthy numbers

A single run is not reliable — **TTFB** especially, since it depends on Akamai edge
cache state (swings of roughly 20× have been observed between cold and warm). For
real measurements:

- Run each URL **5 or more times** and take the **median**. Set this per condition with
  `runs:` in `targets.yaml`, or `--runs 5` on the CLI; the campaign takes the median
  for you and keeps every run's raw artifacts.
- Warm the cache with one throwaway run before measuring.
- Keep the machine and network location consistent between comparisons.

---

## Security notes

- The token lives only in `.env`, which is gitignored; CI fails if `.env` is ever
  tracked and runs `gitleaks` over the full commit history.
- An unset or empty `${VAR}` is a hard `ConfigError` naming the header and the
  variable — but never printing a resolved value. An exported-but-blank token would
  otherwise silently disable the allowlist.
- CR/LF in a resolved value is rejected: a token carrying newlines could smuggle an
  additional header onto every request.
- HAR captures record request headers verbatim. Pass your configured header names to
  `store.artifacts.store_artifacts(..., extra_headers=[...])` so the token is redacted
  on the way into the store, alongside `Cookie` and `Authorization`
  (SECURITY_PLAN.md §2.6). HAR and trace files are gitignored regardless.
- URLs still pass the SSRF gate (`normalize/url_safety.py`) before any navigation;
  headers do not bypass it.
