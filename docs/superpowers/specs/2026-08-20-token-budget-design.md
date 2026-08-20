# Token budget — design

**Date:** 2026-08-20
**Status:** approved, ready for implementation

## Problem

The analysis stage spends Google AI free-tier quota with nothing counting it.
Per page it costs one embedding query, one generation call (two when the JSON
contract fails and the corrective retry fires), and it finishes with one
summarize call plus an embedding batch for `persist_findings`. A six-page
campaign is therefore roughly 36k input and 9k output tokens across ~13
generation requests, and a second run the same day costs the same again.

Today the only defence is reactive: `call_with_quota_backoff` retries a 429 and
then degrades to the rule-based path. By the time that fires the day's quota is
already gone, so the *first* report of the day is not protected — whichever run
happens to be unlucky loses its prose.

**Goal:** guarantee that at least one full report per day can complete on the
free tier, by bounding what the pipeline may spend, with defaults the user can
raise or lower.

## What the free tier actually limits

Google **no longer publishes free-tier numbers** in the API docs. The
[rate-limits page](https://ai.google.dev/gemini-api/docs/rate-limits) states only
that limits are measured across three dimensions — **requests per minute (RPM)**,
**tokens per minute, input (TPM)** and **requests per day (RPD)** — and directs
users to their live values in [AI Studio](https://aistudio.google.com/rate-limit).
Third-party trackers disagree with each other about `gemini-2.0-flash` after the
December 2025 cuts (15 RPM / 1M TPM / 1,500 RPD in the older tables, 5 RPM /
250k TPM in the newer ones).

Two consequences shape this design:

1. **There is no published tokens-per-day limit.** The daily wall is RPD; tokens
   bind per minute. A budget counting only tokens would miss the limit that
   actually stops a run, so the budget counts **requests, input tokens and
   output tokens** — all three.
2. **The numbers cannot be hard-coded with confidence.** Defaults are therefore
   conservative, documented as estimates, and every one of them is overridable
   from config and from the command line.

## Design

### 1. Configuration

New `budget:` block in `config/settings.yaml`, modelled by `BudgetConfig` in
`config/load.py` beside `RagConfig`/`TrendsConfig` and mounted on `Settings`:

```yaml
budget:
  enabled: true
  llm:
    daily_requests: 60
    daily_input_tokens: 250000
    daily_output_tokens: 60000
    max_output_tokens_per_call: 2048
  embeddings:
    daily_requests: 100
    daily_input_tokens: 100000
```

Sized at roughly four times one six-page report under the pessimistic reading of
the free tier, so the first report of a day always fits while a runaway loop
cannot consume the next day's allowance. `max_output_tokens_per_call` is
enforced twice: as the reservation's worst-case output cost, and as the API's
own `max_output_tokens` on the generation request — the only way to bound output
tokens rather than merely observe them.

All limit fields are `ge=0`; zero means "no budget for this service", which
behaves exactly like an exhausted budget. `enabled: false` disables accounting
entirely and restores today's behaviour.

### 2. `rag/budget.py`

Two objects, deliberately separate so the policy can be tested without SQLite.

**`DailyLedger`** — persistence. A `token_ledger` table in the run store,
created the way `embedding_cache` is:

```sql
CREATE TABLE IF NOT EXISTS token_ledger (
    day_utc       TEXT NOT NULL,
    service       TEXT NOT NULL,      -- 'llm' | 'embeddings'
    model         TEXT NOT NULL,
    requests      INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day_utc, service, model)
);
```

`spent(day, service) -> Spend` sums across models, so switching model mid-day
does not reset the day's spend. `add(day, service, model, requests, input,
output)` is an upsert. Rows are keyed by UTC day, taken from an injected clock
so tests can cross midnight deterministically. `InMemoryLedger` implements the
same two methods and is the default when no store is available, so nothing in
the module hard-depends on SQLite.

**`TokenBudget`** — policy.

- `reserve(service, *, estimated_input, estimated_output)` raises
  `BudgetExhaustedError` if today's spend plus this call's worst case would
  exceed any of the three limits for that service. Worst-case output is
  `max_output_tokens_per_call`; worst-case input is the local estimate. Nothing
  is written on reserve — a refused call costs nothing, and a reservation that
  is never spent must not leak.
- `record(service, model, *, input_tokens, output_tokens)` writes one request
  and the actual counts to the ledger after the call returns.
- `remaining(service) -> Remaining` backs `--budget-status` and the end-of-run
  line.
- `estimate_tokens(text) -> int` is `ceil(len(text) / 4)`, the standard rough
  ratio. It decides only whether to *start* a call; the ledger is always
  corrected from the response afterwards, so estimation error never compounds
  across a day.

`BudgetExhaustedError` subclasses `EmbeddingError` so existing
`except EmbeddingError` handlers keep degrading correctly, but every handler that
should name the reason is updated explicitly.

Pre-flight counting is local rather than Google's `countTokens` endpoint: that
endpoint is itself a request against the same free-tier request limits, so
metering with it would spend the thing being rationed.

### 3. Integration points

Both Google clients gain an optional `budget: Optional[TokenBudget] = None`
kwarg. When it is `None` they behave exactly as they do today.

**`analysis/llm.py`** — `_call` reserves before the transport runs and records
after it returns. The placement matters: `_generate_validated` can call `_call`
twice (the corrective retry) and `call_with_quota_backoff` can retry a 429, and
both are real requests that must be counted. The real transport passes
`max_output_tokens` in the generation config and returns usage alongside the
text, so `_call` can record actual `prompt_token_count` /
`candidates_token_count`. Injected test transports may return a bare string; the
client accepts either shape and falls back to the estimate when usage is absent.

**`rag/embeddings.py`** — `_call` reserves and records per batch, with output
tokens always zero. The cache is consulted first, so cached text costs no
budget — already true, and it stays true.

**`analysis/findings.py`** — one more clause beside the 429 handler:

```python
except BudgetExhaustedError:
    return _rule_based_page(ordered_runs, primary, list(symptoms), corpus,
                            "budget_exhausted")
```

Pages already analysed keep their prose; the rest fall back. `run_analysis`
already refuses the summarize call unless every page came back `llm`, so an
exhausted budget mid-campaign cannot produce a model-written summary over
rule-based pages.

**`analysis/reportmodel.py`** — no schema change. `meta.degradation_reason` is
free text, so `budget_exhausted` flows through and the committed skeleton
baseline that `--skeleton-check` enforces is untouched.

**`analysis/__main__.py`** — `_build_live_clients` constructs the ledger from
`settings.storage.sqlite_path` and the budget from `settings.budget`, and passes
it to both clients.

### 4. Command line

On `python -m analysis`, and therefore on `python -m cli analyze`, which
forwards argv verbatim:

| Flag | Effect |
| --- | --- |
| `--no-budget` | Disable accounting for this run |
| `--daily-requests N` | Override `budget.llm.daily_requests` |
| `--daily-input-tokens N` | Override `budget.llm.daily_input_tokens` |
| `--daily-output-tokens N` | Override `budget.llm.daily_output_tokens` |
| `--max-output-tokens N` | Override `budget.llm.max_output_tokens_per_call` |
| `--budget-status` | Print today's spend and remaining, then exit 0 |

`--budget-status` makes no API call and needs no key: it reads the ledger, prints
both services, and returns before any analysis begins.

Every run that used a budget ends with one stderr line:

```
budget: llm 38.2k/250k in, 7.1k/60k out, 9/60 req · embeddings 2.1k/100k in, 7/100 req (today, UTC)
```

### 5. Failure and degradation

| Situation | Behaviour |
| --- | --- |
| Budget exhausted before a page's generation call | That page degrades to rules, `degradation_reason="budget_exhausted"` |
| Budget exhausted before an embedding batch | Retrieval returns no hits for that page; the page degrades the same way |
| Budget exhausted before `persist_findings` | The report is already written; the existing "findings were not persisted" warning covers it |
| Ledger unwritable (locked or read-only DB) | Warn once to stderr, fall back to `InMemoryLedger`; a bookkeeping failure must never lose a report |
| `enabled: false` or `--no-budget` | No reservations, no ledger writes, no summary line |

The rule the whole feature obeys: **a report always comes out.** Budgeting can
change a report's mode; it can never fail a run.

### 6. Testing

Offline, matching how the existing Google clients are tested — injected
transports, no SDK, no key.

- `tests/unit/budget_test.py` — estimation; reservation refused on each of the
  three limits independently; reconciliation from actual usage; UTC day rollover
  via injected clock; spend summed across models within a day; `InMemoryLedger`
  and `DailyLedger` behaving identically against the same sequence.
- `tests/unit/analysis_llm_test.py` — the corrective retry counts as two
  requests; usage recorded from `usage_metadata` when present and from the
  estimate when absent; `max_output_tokens` reaching the transport.
- `tests/unit/rag_test.py` — cached text costs no budget; one batch reserves once.
- `tests/integration/analysis_pipeline_test.py` — a campaign whose budget is
  exhausted after the first page still writes a report, with that page in `llm`
  mode, later pages rule-based, and `meta.degradation_reason ==
  "budget_exhausted"`.
- config tests — defaults load, overrides parse, negative values rejected.

### 7. Documentation

- `README.md` — a "Token budget" subsection under configuration, the CLI flags,
  and the project-status section.
- `docs/PROJECT_SPEC.md` — the budget block in the settings reference.
- `config/settings.yaml` — inline comments recording that the free-tier numbers
  are estimates, that Google no longer publishes them, and where the live values
  are.

## Out of scope

- Per-minute (RPM/TPM) pacing. The free tier's per-minute limits are already
  handled reactively by `call_with_quota_backoff`, and pacing to them would slow
  every campaign to protect against an error that is already recoverable.
- Cost estimation in currency. Free tier only; a paid-tier price model would be
  guesswork.
- Budgeting the ingestion stage. It makes no API calls.
