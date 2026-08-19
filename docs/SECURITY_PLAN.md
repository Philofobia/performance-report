# Security Plan — Performance RAG & Reporting System

> **Status:** Draft · **Owner:** m.parisi · **Companion skill:** `.agents/skills/security/SKILL.md`
> Applies to every module; the `security` skill is the operational checklist.

## 1. Scope & threat model
This system ingests web performance data (manual + automated browser), stores it,
runs LLM analysis (Google AI API), and renders PDF/MD reports. Assets: the Google
API key, captured HAR/screenshots, and report data. Threats: secret leakage,
SSRF via the browser layer, prompt injection into the RAG/LLM pipeline, template
injection in report rendering, and known-vulnerable dependencies.

| Surface | Risk |
|---|---|
| `.env` API key | committed/hard-coded → cost abuse, account compromise |
| Browser ingestion | SSRF — user URLs point at private/internal hosts |
| RAG/LLM | prompt injection from untrusted retrieved content |
| Report render | HTML/template injection from LLM strings |
| Storage/artifacts | tokens/secrets captured in HAR/screenshots |
| Dependencies | known CVEs in pinned packages |

## 2. Controls (with verification)
### 2.1 Secrets
- Key only in gitignored `.env`; `.env.example` = placeholders.
- CI/pre-commit fails if `.env` in `git status` or key pattern `AIza[0-9A-Za-z_-]{35}` committed.
- Key never logged; generic error messages on failure.
**Verify:** `gitleaks detect` clean; `git status` shows no `.env`/secrets.

### 2.2 SSRF (browser ingestion)
Reject non-`https`; block `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`,
`169.254/16`, `::1`, `fc00::/7`, `0.0.0.0/8`; forbid raw-IP + userinfo URLs;
optionally enforce a target allow-list from `config/targets.yaml`.
Implement in `normalize/url_safety.py`; unit-test all ranges.
**Verify:** `url_safety` unit tests green covering each blocked category.

- **Where navigation ends is checked too.** `validate_url` runs before `goto`
  and can only judge the URL we asked for. A public page answering
  `302 -> https://192.168.1.1/` puts an internal response in front of the
  collectors, and the block-page guard does not catch it — an internal host can
  answer a healthy 200. `runner.assert_safe_chain` walks Playwright's
  `redirected_from` links after navigation and re-runs the guard on every hop.
- **The redirect half is detection, not prevention** — accepted limitation.
  The hop is already fetched by the time Playwright reports it; the run is
  refused so nothing derived from it reaches the report or the store, but the
  request did happen. Preventing it means intercepting every request with
  `page.route`, which adds interception latency to the very numbers this tool
  exists to measure. Same reasoning covers DNS rebinding between our lookup and
  the browser's.

### 2.3 Prompt-injection defense
Retrieved web/KB content is **untrusted reference material**; system prompt
instructs the model to ignore instructions in it; context delimited from
instructions; no secrets in prompts; free-form problem text sanitized/truncated
and kept out of the system block.

### 2.4 Input validation
Pydantic canonical schema on all inputs; strict metric units/ranges; URL
validation via 2.2; length caps on problem text and matrix sizes.

### 2.5 Template/rendering safety
HTML report template escapes all LLM-derived content (no `|safe`/raw on
untrusted data); test asserts no unescaped `<script>` survives malicious input.

### 2.6 Artifact/secret hygiene
HAR/screenshots may contain cookies/tokens — scrub `Cookie`/`Authorization` from
stored HAR or keep artifacts in gitignored `data/`; document in README.

- **The scrub is on the campaign's path.** `store.artifacts.store_artifacts`
  runs per completed (page × condition), from `ingest/persist.py`, and moves
  rather than copies — a copy leaves the unredacted original on disk, which is
  the whole thing the scrub exists to prevent. Every header name configured in
  `targets.yaml` is passed as `extra_headers`, so a bot-allowlist token is
  redacted alongside the built-in credential headers. Until this was wired,
  the scrubber was reached only by its own tests and `data/raw` kept live
  session cookies indefinitely.

- **Appendix path confinement.** `report/images.py:embed_png` resolves every
  screenshot path and refuses anything outside `settings.storage.raw_dir`. The
  path arrives from `report.json`, a file on disk a user can edit; a renderer
  that base64s any path it is handed into a shareable document is a
  file-disclosure primitive.
- **Screenshots are page contents.** A capture of an authenticated page shows
  whatever was on screen — cart contents, an email address, an order number —
  and the PDF gets emailed. `report --no-appendix-images` renders path-only
  rows so the answer to "can I share this?" is not "re-run the campaign".
- **HAR URLs are re-redacted at render.** The stored HAR is scrubbed on the way
  in, but a HAR written before a scrubbing rule existed is still in the store,
  so `analysis/appendix.py` re-applies `store.artifacts.redact_url`.

### 2.7 Dependency & secret scanning (CI)
`pip-audit -r requirements.txt` (fail on high/critical);
gitleaks (pinned version) over both the working tree and full commit history —
a secret committed and later removed is still leaked;
pin `requirements.txt`; scheduled dependency updates + advisory review.

**No suppressions.** CI must fail on a real advisory rather than pass via
`--ignore-vuln`. When an advisory has no fixed release, we change the dependency
instead of silencing the finding. Applied so far:

| Package | Finding | Resolution |
|---|---|---|
| `chromadb` | `CVE-2026-45829` pre-auth RCE (CVSS 10.0), 1.0.0-1.5.9, **no patch**, maintainers unresponsive | **Removed.** Replaced by SQLite BLOBs + numpy exact search (PROJECT_SPEC §8.1) — dropped 84 packages including the vulnerable FastAPI server stack |
| `litellm` | 19 known CVEs at the pinned 1.40.0 | **Removed.** Unused and spec-optional; `google-genai` covers embeddings + generation |
| `matplotlib` | pinned to 3.10.4, which was never published | Corrected to 3.11.1 (this broke the CI install outright) |

**Minimise transitive surface.** Prefer a dependency we do not need at all over
one we must monitor. Both removals above deleted attack surface that our design
never exercised but would still have installed.

### 2.8 Logging/error redaction
No key, no userinfo URLs, no report contents in logs; generic user-facing errors.

## 3. Review workflow
On request, run in order: (1) secrets check → (2) SSRF on new URL paths →
(3) injection review on prompt/template changes → (4) dependency scan →
(5) confirm `component-testing` covers hardened paths. Report per finding as
**Severity · Location · Risk · Fix**; mark pass only when each control is
satisfied or a limitation is explicitly documented.

## 4. Definition of done (security)
- [ ] `.gitignore` verified; no secrets committed; key-pattern scan clean.
- [ ] `url_safety` implemented + unit-tested (all private ranges blocked).
- [ ] All prompts delimit untrusted content; system prompt resists indirect injection.
- [ ] Report template escapes all dynamic content; injection test green.
- [ ] HAR scrubbing or gitignored-artifact decision documented.
- [ ] `pip-audit` + `gitleaks` clean in CI.
