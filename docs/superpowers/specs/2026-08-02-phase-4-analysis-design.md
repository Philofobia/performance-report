# Phase 4 — Analysis + Report Model

**Date:** 2026-08-02
**Status:** Approved design, not yet implemented
**Covers:** PROJECT_SPEC.md §10 Phase 4 — `analysis/llm.py`, `analysis/findings.py`,
`analysis/estimator.py`, `analysis/reportmodel.py`

---

## 1. Purpose

Phases 0–3b get data in and make it retrievable. Nothing comes out. Phase 4 closes
that half: it turns a campaign's runs plus their retrieved playbooks into a
**Report JSON** — the deterministic document that Phase 5's template renders and
nothing else in the system is allowed to invent.

Two guarantees define the phase:

- **§6.2 determinism.** Everything except prose is rule-derived and
  deterministically ordered. Same runs in, byte-identical JSON out (modulo the
  explicitly volatile fields in §7).
- **§11 no hallucinated magnitudes.** The model chooses *which* playbook applies.
  Code supplies *the number*. These are separate code paths, and the one that
  produces numbers cannot read the model's prose.

## 2. Scope

**In:**

- `analysis/llm.py` — Google generation client with strict JSON contract
- `analysis/findings.py` — per-page analysis, LLM path and rule-based path
- `analysis/estimator.py` — pure projection math
- `analysis/reportmodel.py` — Report JSON assembly
- `analysis/__main__.py` — `python -m analysis`, runs in → `report.json` out
- Persisting findings into the vector store as `kind="finding"`

**Out (deferred, with reason):**

- A `reports` table in SQLite. Phase 5 reads `report.json` from disk; the table
  has no consumer until Phase 7's trend comparison defines what needs querying,
  and that will shape its schema. Deferred to Phase 7.
- The unified `src/cli.py`. Phase 6. `python -m analysis` is the interim seam and
  is expected to be absorbed.
- Any rendering — HTML, charts, PDF, Markdown. Phase 5.

## 3. Module boundaries

| File | Responsibility | Depends on |
|---|---|---|
| `analysis/llm.py` | Generation client: injected transport, temperature 0, quota backoff, JSON extraction, Pydantic validation, one retry on malformed output | `google-genai` (lazy import) |
| `analysis/findings.py` | Per-page orchestration: primary-run selection, retrieval, prompt, model call **or** rule-based fallback, citation validation | `rag/*`, `analysis/llm.py` |
| `analysis/estimator.py` | Playbook range → projected before/after. Pure functions, no I/O, no config reads, no LLM | stdlib only |
| `analysis/reportmodel.py` | Assembles and serialises the Report JSON | the above, `config/load.py` |
| `analysis/__main__.py` | CLI entry point | the above, `store/*` |

`estimator.py` is deliberately pure. §11's mitigation is only real if the code
producing improvement numbers has no access to the model's output text. Purity
also makes the math unit-testable without a store, a key, or a fake transport.

## 4. Data flow

```
run JSONs (data/processed/*.json) or SQLite runs table
  │
  └─ group by page.name
       │
       └─ per page:
            primary run  = worst condition (see §4.1)
            detect_symptoms(run, thresholds)        [rag/retrieve, existing]
            retrieve_context(run, store, client)    [rag/retrieve, existing]
            retrieve_prior_findings(...)            [existing, opt-in --use-priors]
            build_analysis_prompt(...)              [rag/prompt, existing]
            llm.analyze() → LlmPageAnalysis   OR   rule_based_analysis()
            estimator.project(...)                  [attaches every number]
            └─► PageAnalysis
  │
  ├─ summary call over the per-page findings only, no re-retrieval → ExecutiveSummary
  │
  └─► ReportModel
        ├─► data/reports/<campaign_id>/report.json
        └─► findings embedded into the vector store as kind="finding"
```

### 4.1 Primary-run selection

A page may have several conditions (mobile/slow-4g, desktop/fast-3g). One is
analysed in depth; the rest appear as comparison data in the prompt and in the
report's cross-page table.

The primary run is chosen by, in order:

1. most `severity == "fail"` symptoms (descending)
2. `lcp_ms` (descending; `None` sorts last)
3. `run_id` (ascending — a total order, so ties never depend on input ordering)

This is pure function `select_primary(runs) -> Run` in `findings.py`, unit-tested
against ties.

### 4.2 Campaign identity

`campaign_id = f"{project_slug}-{sha256('\n'.join(sorted(run_ids)))[:8]}"`

`project_slug` is the project name lowercased with every run of non-alphanumeric
characters collapsed to `-` and stripped at the ends — the same `_slug()` rule
`rag/knowledge.py` already uses. It reaches the filesystem as a directory name,
so slugification is a safety requirement, not cosmetics (§12).

Derived from content, not the clock, so the determinism test can run the pipeline
twice and compare outputs directly.

## 5. LLM contract

### 5.1 Output shape

The model must return a single JSON object. Anything before or after it (fenced
code blocks, preamble) is stripped by extracting the outermost balanced `{...}`.

```jsonc
{
  "summary": "one paragraph, what is wrong with this page",
  "findings": [
    {
      "title": "Hero video dominates the LCP path",
      "detail": "prose",
      "evidence": ["lcp_ms=6200", "hero.mp4 2140KB"],
      "symptom_codes": ["lcp_fail", "dominant_media"]
    }
  ],
  "impacts": [
    { "audience": "ux", "text": "..." },
    { "audience": "seo", "text": "..." },
    { "audience": "business", "text": "..." }
  ],
  "recommendations": [
    {
      "title": "Replace the autoplaying hero video with a poster image",
      "rationale": "prose",
      "playbook_source": "images.md",
      "playbook_section": "Serve modern formats"
    }
  ]
}
```

Pydantic models `LlmFinding`, `LlmImpact`, `LlmRecommendation`, `LlmPageAnalysis`
in `llm.py`. `audience` is `Literal["ux", "seo", "business"]`.

Notably absent: any numeric field. The model is given no place to put a
magnitude, which is a cheaper defence than deleting one after the fact.

### 5.2 Validation and the citation rule

1. Extract JSON → parse → Pydantic validate.
2. Failure at any step: retry **once** with the parse error appended as a
   correction turn. Second failure → rule-based fallback for that page.
3. `playbook_source` must match the `source` of one of the retrieved hits.
   A recommendation citing anything else is **dropped**, not repaired — a
   fabricated citation is a fabricated recommendation. Drops are counted in
   `meta.dropped_recommendations`.
4. `symptom_codes` are intersected with the codes actually detected; unknown
   codes are dropped from the finding but the finding survives.
5. A page whose recommendations are *all* dropped falls back to rule-based.

### 5.3 Client behaviour

Mirrors `rag/embeddings.py` so the codebase has one shape for "calls Google":

- `transport` is injected — `(messages, model, config) -> str`. Tests never need
  the SDK or a key.
- Default transport built lazily via `google.genai`, key through
  `resolve_api_key()` (reused from `rag.embeddings`).
- `temperature=0`, fixed `top_p`, and `response_mime_type="application/json"`
  where the SDK supports it.
- 429 → exponential backoff with jitter via `backoff_delays()`, then
  `QuotaExceededError`.
- Typed errors: `AnalysisError` base, `LlmUnavailableError`,
  `InvalidModelOutputError`. `MissingApiKeyError` / `QuotaExceededError` are
  re-used from `rag.embeddings` rather than redefined.

### 5.4 Executive summary call

One additional call. Input: the per-page `summary` and `findings[].title` from
every page, plus the cross-page metric table. No retrieval, no playbooks — it
synthesises what the page calls already produced.

Output:

```jsonc
{ "problem": "...", "key_finding": "...", "top_actions": ["...", "...", "..."] }
```

`top_actions` holds **at most 3** entries, validated as `1 <= len <= 3`. If the
model returns more, the list is truncated to 3. If it returns fewer than the
campaign has recommendations, it is topped up from the highest-projected
recommendations not already named. If the campaign has fewer than 3
recommendations in total, `top_actions` is shorter — the list is never padded
with invented actions.

## 6. Estimator

Pure module. Input: validated recommendations, the primary run's metrics, and the
front-matter metadata carried on each `SearchHit`. Output: `Projection` objects.

### 6.1 Where the numbers come from

Playbook front matter, already parsed by `rag/knowledge.py` and carried through
`Chunk.metadata → Document.metadata → SearchHit.metadata`:

```
expected_lcp_reduction_pct: 15, 40
expected_ttfb_reduction_pct: 30, 80
effort: low
```

Key form is `expected_<metric>_reduction_pct` → `(low, high)`. Absolute-delta
keys (`expected_cls_reduction_abs: 0.05, 0.15`) are supported for CLS, where a
percentage of a unitless ratio reads poorly. If the store's metadata lacks front
matter (an old index), fall back to re-reading the playbook file via
`knowledge.load_playbook`; if that also yields nothing, magnitude is `unknown`.

### 6.2 Math

Per metric, recommendations affecting it are sorted by low-bound descending
(then `playbook_source` ascending for ties) and applied sequentially:

```
value_0 = measured value
pct_n   = low_bound_n × DECAY^(n-1)          DECAY = 0.8
value_n = value_(n-1) × (1 − pct_n)
```

Constraints:

- Cumulative reduction per metric is capped at `MAX_TOTAL_REDUCTION = 0.70`.
  Four stacked fixes do not take a metric to near zero.
- Projected values are clamped to `>= 0`.
- Absolute-delta metrics subtract rather than scale, with the same decay and cap
  applied to the delta.
- A recommendation with no range for any metric the run measured gets
  `magnitude = "unknown"`, is **excluded from stacking**, and is still listed —
  matching the system prompt's rule 3 ("if a playbook gives no range, say the
  magnitude is unknown").

### 6.3 Output

```python
@dataclass
class Projection:
    metric: str            # "lcp_ms"
    before: float
    after_low: float       # conservative — drives the chart and the headline
    after_high: float      # optimistic end of the playbook's band
    reduction_pct: float   # from after_low
    source: str            # "images.md", or "aggregate"
```

`effort` is a property of the recommendation, not of the projection, so it lives
on the recommendation only — an aggregate projection spans several playbooks and
has no single effort level.

Plus `aggregate_projection(projections) -> dict[metric, Projection]` for §6's
aggregated before→after chart, where `source` becomes `"aggregate"`.

Rationale for the conservative default: an under-promise that lands is worth more
than a midpoint that misses. The band is still reported, so nothing is hidden.

## 7. Report JSON

Pydantic models in `reportmodel.py`. `schema_version: 1`. Structure mirrors §6's
sections 0–7 one-to-one so Phase 5's template is a pure transform with no
computation of its own.

```jsonc
{
  "schema_version": 1,
  "cover": {                                    // §6 section 0
    "project": "storefront",
    "campaign_id": "storefront-9f3ab120",
    "generated_at": "2026-08-02T14:30:00Z",     // volatile
    "pages": ["homepage", "plp"],
    "verdict": "fail"                            // worst severity across pages
  },
  "summary": {                                   // §6 section 1
    "problem": "...", "key_finding": "...",
    "top_actions": ["...", "...", "..."]
  },
  "pages": [                                     // §6 sections 2-6, repeating block
    {
      "name": "homepage",
      "url": "https://example.com/",
      "primary_run_id": "run_...",
      "conditions": [ { "device": "...", "network": "...", "runs": 3,
                        "run_id": "run_...", "metrics": { ... } } ],
      "metrics": { "cwp": {...}, "network": {...}, "main_thread": {...},
                   "lighthouse": {...} },
      "targets": { "lcp_ms": 2500, "cls": 0.1, "inp_ms": 200 },
      "symptoms": [ { "code": "lcp_fail", "text": "...", "severity": "fail",
                      "metric": "lcp_ms", "value": 6200, "target": 2500 } ],
      "resources": [ { "name": "/hero.mp4", "type": "media",
                       "transfer_kb": 2140, "duration_ms": 390 } ],
      "resource_type_totals": { "media": 2140, "img": 610, "script": 480 },
      "findings": [ { "title": "...", "detail": "...", "evidence": [...],
                      "symptom_codes": [...] } ],
      "impacts": [ { "audience": "ux", "text": "..." } ],
      "recommendations": [ { "title": "...", "rationale": "...",
                             "playbook_source": "images.md",
                             "playbook_section": "...",
                             "effort": "low",
                             "projection": { ...Projection... } } ],
      "projections": { "lcp_ms": { ...aggregate... } }
    }
  ],
  "comparison": [                                // cross-page table, §6.1
    { "page": "homepage", "device": "mid-mobile", "network": "slow-4g",
      "lcp_ms": 6200, "cls": 0.42, "inp_ms": 480, "tbt_ms": 620,
      "verdict": "fail" }
  ],
  "methodology": {                               // §6 section 7
    "devices": [...], "networks": [...], "runs_per_condition": 3,
    "captures": [ { "page": "homepage", "screenshot": "...", "har": "...",
                    "trace": "..." } ],
    "thresholds": { ... }
  },
  "meta": {
    "analysis_mode": "llm",                      // or "rule_based"
    "degradation_reason": null,                  // populated when rule_based
    "model": "gemini-2.0-flash",
    "playbooks_cited": ["images.md", "caching.md"],
    "dropped_recommendations": 0,
    "knowledge_digest": "sha256..."              // from knowledge.content_digest
  }
}
```

**Volatile fields**, excluded from the determinism comparison:
`cover.generated_at`. Everything else — including ordering of every list — must
be reproducible.

### 7.1 Deterministic ordering

- `pages`: by page name ascending.
- `conditions`, `comparison`: by (device, network, run_id) ascending.
- `symptoms`: already ordered by `detect_symptoms` (severity, then code).
- `recommendations`: by projected absolute reduction descending, then
  `playbook_source` ascending, then `title` ascending. **Not** the model's order —
  the model's ordering is not stable across calls.
- `resources`: by `transfer_kb` descending, then name ascending (matches
  `rag/prompt.format_resources`).
- `playbooks_cited`: sorted, deduplicated.

### 7.2 Verdict

Per page: `fail` if any `fail` symptom, else `warn` if any `warn`, else `pass`.
Cover verdict is the worst across pages. Thresholds come from
`config/settings.yaml`, never hard-coded — §6.2's "computed from thresholds".

## 8. Degradation

`analyze` always produces a valid Report JSON. It never fails because a model was
unreachable.

| Trigger | Behaviour |
|---|---|
| No `GOOGLE_API_KEY` | Whole campaign rule-based; `degradation_reason: "no_api_key"` |
| 429 after retries | Remaining pages rule-based; reason `"quota_exhausted"` |
| Malformed JSON twice | That page rule-based; reason `"invalid_model_output"` |
| All recommendations dropped as uncited | That page rule-based; reason `"no_grounded_recommendations"` |

Embeddings are a separate dependency: with no key, retrieval cannot run either, so
the rule-based path selects playbooks by **front-matter `symptoms:` matching**
against detected symptom codes, reading `data/knowledge/` from disk. No embeddings,
no network, fully deterministic.

Rule-based prose is templated from symptom text — e.g. a `lcp_fail` symptom
becomes a finding titled "Largest Contentful Paint exceeds the target" with the
symptom's own sentence as detail. Impacts come from a fixed
symptom-code → impact-statement mapping. The result reads as a competent
threshold report, and `meta.analysis_mode` states plainly that no model reasoned
over it.

Consequence worth stating: the report is honest about being degraded, but the
`analysis_mode` field is the only signal. Phase 5 must surface it on the cover;
that is a Phase 5 requirement recorded here.

## 9. Persisting findings

After a successful analysis, each page's findings are embedded as vector-store
documents with `kind="finding"`, closing the loop `retrieve_prior_findings()`
already expects.

- `doc_id`: `finding:{campaign_id}:{page_name}`
- `source`: `{project}/{page_name}`
- `text`: the page summary plus each finding's title and detail
- `metadata`: `campaign_id`, `page`, `run_id`, `created_at`, `symptom_codes`

Retrieval of priors stays **opt-in** (`--use-priors`, default off) so a first run
and a repeat run behave identically unless the user asks otherwise, and so the
determinism test is not perturbed by store state. Skipped entirely in rule-based
mode — there is no LLM finding worth remembering, and embedding requires the key
that was missing.

## 10. CLI

```bash
python -m analysis                              # all runs in data/processed
python -m analysis --pages homepage,plp
python -m analysis --input-dir data/processed
python -m analysis --from-store                 # read the SQLite runs table
python -m analysis --output-dir data/reports
python -m analysis --no-llm                     # force rule-based
python -m analysis --use-priors                 # include prior findings
python -m analysis --top-k 5                     # default: settings.rag.top_k
```

`--input-dir` and `--from-store` are mutually exclusive; passing both is a usage
error. Defaults come from `config/settings.yaml` — `storage.sqlite_path`,
`report.output_dir`, `rag.top_k`, `models.llm`, and `thresholds` — never
hard-coded in the CLI.

Writes `data/reports/<campaign_id>/report.json` and prints the path plus a
one-line verdict summary. Exit code 0 on a produced report (including degraded),
non-zero only on a genuine error — no runs found, unreadable input, unwritable
output directory, or conflicting flags.

## 11. Testing

All offline, matching the existing suite's injected-seam style. No test requires
a key, a network, or a browser.

**`tests/unit/test_estimator.py`** — pure math:
- single recommendation, low/high bounds
- stacking with decay: three 20% wins on one metric
- 70% cumulative cap
- absolute-delta path for CLS
- missing range → `unknown`, excluded from stacking, still returned
- unknown metric name ignored rather than raising
- aggregate over an empty list returns empty, not a crash

**`tests/unit/test_llm.py`** — fake transport:
- valid JSON → validated model
- JSON wrapped in a fenced block → extracted
- malformed once → retry → success
- malformed twice → `InvalidModelOutputError`
- 429 → backoff schedule asserted with injected jitter → `QuotaExceededError`
- transport never receives the API key in a log call

**`tests/unit/test_findings.py`**:
- `select_primary` ordering including ties
- recommendation citing an unretrieved playbook is dropped and counted
- all-dropped → rule-based fallback for that page
- rule-based path with no store and no client produces findings and
  recommendations from front-matter symptom matching
- unknown `symptom_codes` pruned, finding retained

**`tests/unit/test_reportmodel.py`**:
- golden Report JSON from a fixed two-page campaign
- every ordering rule from §7.1
- verdict derivation from thresholds
- `analysis_mode` and `degradation_reason` populated correctly

**`tests/integration/test_analysis_pipeline.py`**:
- **determinism**: run the pipeline twice over identical runs with a fake LLM,
  assert JSON equality after removing `cover.generated_at`
- degraded run with no client produces a schema-valid report
- findings persisted to a fake store with the expected `doc_id` and `kind`

Coverage stays above the CI floor of 80%.

## 12. Security

Nothing in this phase widens the attack surface, but three points carry over:

- **Prompt injection.** `build_analysis_prompt` already delimits and neutralises
  untrusted context (SECURITY_PLAN §2.3). Phase 4 adds no new path from untrusted
  text to the system block. The summary call receives only text this system
  generated from validated model output — but that output originated from a model
  that read untrusted context, so summary input is neutralised with the same
  `prompt.neutralize()` before use.
- **Secrets.** The key is resolved through the existing `resolve_api_key()` and
  never logged, never placed in the Report JSON, never in an error message.
- **Model output is untrusted input.** It is parsed as JSON with a strict schema,
  never `eval`'d, and never used to build a file path. `campaign_id` and page
  names in output paths are derived from config and run data, and slugified
  before touching the filesystem.

## 13. Definition of done

- [ ] `python -m analysis` over a real two-page campaign writes a schema-valid
      `report.json` covering every tested page
- [ ] Same command with no API key writes a schema-valid report with
      `analysis_mode: "rule_based"`
- [ ] Determinism test passes: identical runs → identical JSON minus
      `cover.generated_at`
- [ ] Estimator magnitudes trace to playbook front matter in every case; no
      number in the report originates in model prose
- [ ] A recommendation citing an unretrieved playbook is dropped, and the drop is
      visible in `meta.dropped_recommendations`
- [ ] `pytest -m "not e2e"` green, coverage ≥ 80%
- [ ] PROJECT_SPEC §10 Phase 4 checkboxes ticked; README's "Where the project is"
      updated to state that analysis produces a Report JSON and that rendering is
      still missing
