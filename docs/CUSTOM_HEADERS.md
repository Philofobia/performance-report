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

## Which requests carry the header

**Every request to the page's own site and its subdomains, and nothing else.** For
`https://www.oakley.com/...` that is `oakley.com` and `*.oakley.com` — the document,
and `media.oakley.com` / `assets2.oakley.com`, which Akamai also fronts. No third
party ever receives it (`install_site_headers` in `ingest/browser/runner.py`).

It used to be set on the whole browser context, which sends it to *every* host. That
was wrong in two ways, both found on the live Oakley homepage (2026-09-28):

- **It broke the measurement.** A non-safelisted header on a cross-origin fetch, XHR
  or `crossorigin` image forces a CORS preflight. A preflight never carries the token,
  and hosts that do not allow the header fail the request. Every `media.oakley.com`
  image failed (`net::ERR_FAILED`, then `ERR_BLOCKED_BY_ORB`), the hero never painted,
  and LCP fell back to text that appears late: **6–9 s measured, against ~2 s in
  WebPageTest and in the field**. Scoped to the site, the hero is the LCP element
  again at ~2.7 s under mid-mobile / slow-4G.
- **It leaked the token** to forter, google, doubleclick, affirm, cookielaw and every
  other third party on the page.

WebPageTest runs configured with a custom header have the same problem: their console
shows `Request header field x-akamai-bot is not allowed by Access-Control-Allow-Headers
in preflight response` for cookielaw, forter and others. Failures of those requests in
such a test are the test's, not the site's.

The header is added by request interception, which applies it below the CORS layer —
no preflight — and keeps cookies. The price is interception latency on first-party
requests; a run with no headers configured installs no interception at all. The scope
is the host minus a leading `www.`, never a guessed registrable domain: guessing would
turn `shop.example.co.uk` into `co.uk`.

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
