# Grafana field-data ingestion — design

**Date:** 2026-08-24
**Status:** approved, ready for implementation

## Problem

Every number in the report today is **lab** data: a headless Chromium campaign
driving configured pages under emulated device and network conditions. That
buys reproducibility and causal detail — a HAR, a main-thread breakdown, a
screenshot — but it cannot answer the question the reader actually has, which
is whether any of it matters to real visitors.

A Grafana dashboard already answers that question against ClickHouse-backed
mPulse RUM data: bounce rate, conversion, pages per session, rage clicks,
frustration index, field Core Web Vitals at p75/p95, and breakdowns by device,
country and page type. None of it reaches the report. The analyst reads two
artifacts and correlates them by hand, every time.

**Goal:** fetch that field data over Grafana's API, persist it beside the runs,
feed it to the RAG layer as trusted measurement, and print it in the report —
both brand-wide and joined onto each tested page — so a single artifact says
what the synthetic measurement was *and* what real users experienced.

## What Grafana's API will and will not do

The dashboard's panel SQL is **not directly executable** through the API, and
this shapes the whole ingestion module.

`POST /api/ds/query` forwards `rawSql` to the ClickHouse datasource plugin,
which expands **datasource macros** server-side — `$__timeFilter(timestamp)`
and `$__timeInterval(timestamp)` both work unchanged. But **dashboard template
variables are interpolated by the browser**, in the Grafana frontend, before a
query is ever dispatched. An API caller gets no such treatment: `${brand:sqlstring}`,
`${brand_group:sqlstring}` and `${mpulse}` arrive at ClickHouse as literal text
and the query errors.

Two consequences:

1. **This module owns variable substitution.** The host filter and the table
   name are rendered into the SQL before dispatch.
2. **The brand filter becomes configuration, not a dropdown.** There is no
   interactive picker in a batch pipeline, so the hosts have to come from
   somewhere the project already knows about — see §2.

A third consequence is a security obligation. Because we now build SQL by
string substitution against a datasource whose token likely carries broad read
rights, the host list is the sharpest edge in this feature and is validated
before it is quoted (§3.3).

## Design

### 1. Configuration

New `grafana:` block in `config/settings.yaml`, modelled by `GrafanaConfig` in
`config/load.py` beside `RagConfig` and `TrendsConfig`, mounted on `Settings`:

```yaml
grafana:
  # Base URL of the Grafana instance. Must be a public https host: the SSRF
  # guard in normalize/url_safety.py applies to it unchanged.
  base_url: ""
  # ClickHouse datasource uid, from the dashboard JSON.
  datasource_uid: bea6kbi7cbpxcc
  # Fully-qualified table the panels read.
  table: luxotticagroupspa.mpulse
  # Relative window, Grafana syntax. Matches the dashboard default.
  window: 7d
  # Per-request HTTP timeout.
  timeout_s: 30
  # pageGroupName -> targets.yaml page name. See §6.2 — this mapping cannot be
  # inferred and an unmapped page simply renders no field row.
  page_groups:
    Home: homepage
    Plp: plp
    Pdp: pdp
```

The token is **not** configuration. `GRAFANA_TOKEN` is read from `.env`,
following `GOOGLE_API_KEY` and `AKAMAI_BOT_TOKEN`, and `.env.example` gains a
documented blank entry.

### 2. Scoping: hosts come from `targets.yaml`

The `pageDomainName IN (...)` filter is built from the hostnames of the pages
already listed in `config/targets.yaml` — `www.oakley.com` for the current
project — deduplicated and sorted.

This is deliberate. A second list of hosts is a second thing to keep in sync,
and the failure mode when it drifts is silent: the report would compare lab
measurements of one site against field measurements of another and read
perfectly plausibly. Deriving the filter makes that class of error impossible.

The trade-off is accepted: pulling a whole brand group while lab-testing three
US pages is not expressible. If that is wanted later it is an additive
`hosts:` override, not a redesign.

### 3. Fetch layer — `ingest/grafana/`

#### 3.1 `client.py`

`POST {base_url}/api/ds/query` with `Authorization: Bearer <GRAFANA_TOKEN>`,
body:

```json
{
  "from": "now-7d", "to": "now",
  "queries": [
    {"refId": "sessions", "datasource": {"type": "grafana-clickhouse-datasource",
     "uid": "..."}, "rawSql": "...", "format": 0,
     "intervalMs": 3600000, "maxDataPoints": 200}
  ]
}
```

Transport is stdlib `urllib.request`. No new pin: this repo has twice deleted
dependencies nothing imported (`reportlab`, `typer`) and records that it "pins
what it imports". A JSON POST with a bearer header does not justify reversing
that.

The base URL passes through `normalize.url_safety.validate_url()` **unchanged** —
https required, raw IPs and userinfo forbidden, private ranges blocked.
Confirmed with the operator that Grafana is a public https host, so no
exception to SECURITY_PLAN §2.2 is created by this feature.

One retry on 5xx and on connection reset; no retry on 4xx, because a 401 is a
bad token and retrying it just delays the error message that fixes it.

#### 3.2 `queries.py`

The eight SQL statements, each a template with exactly two substitution points:
`{hosts}` and `{table}`. Datasource macros are left verbatim. Derived from the
dashboard panels:

| refId | Panel | Shape |
| --- | --- | --- |
| `sessions` | Bounce Rate, Avg Session Pages & Conversion over Time | timeseries |
| `vitals` | LCP/INP/CLS p75+p95, TTFB, page load | timeseries |
| `frustration` | Rage clicks and frustration over time | timeseries |
| `by_device` | Core Web Vitals per device type + frustration/engagement per device | table |
| `by_country` | Top paesi per CWV + frustration/TTFB per paese | table |
| `by_pagetype` | Core Web Vitals per Page Type + frustration, rage and bounce per group | table |
| `inp_buckets` | Frustrazione per bucket di INP | table |
| `assets` | Asset Type Impact | table |

Three merges reduce the nine panels agreed in design to eight queries.
`by_device` and `by_country` each combine two dashboard panels that differ only
in which columns they select from the same `GROUP BY`; issuing them separately
would double the ClickHouse cost for nothing. `by_pagetype` likewise carries
both the per-page-type CWV columns *and* the frustration/rage/bounce columns
the §6.2 join needs, rather than querying the same grouping twice — which is
why `by_pagetype` rows populate `FieldSnapshot.pages` as well as
`FieldSnapshot.by_pagetype`.

#### 3.3 Host validation before quoting

Every hostname is matched against `^[a-z0-9][a-z0-9.-]*$` **before** being
single-quoted into the `IN (...)` list. A host failing the pattern raises
rather than being escaped-and-passed: `targets.yaml` is operator-authored, so a
hostname containing a quote is a mistake or an attack, and neither should reach
ClickHouse. This is the control that keeps §"What Grafana's API will not do"
from becoming an injection surface.

#### 3.4 `parse.py`

Grafana returns column-major frames:

```
results.<refId>.frames[].schema.fields[]  -> names and types
results.<refId>.frames[].data.values[]    -> parallel arrays, one per field
```

`parse.py` maps those to the typed models by field *name*, never by position,
so a column added to a panel does not silently shift every value one to the
left. A field the schema does not carry yields `None`.

This is the component most likely to diverge from expectation, so it is
developed against a **recorded real response** committed as a fixture.

### 4. Data model — `normalize/field.py`

`FieldSnapshot`, a Pydantic model beside `Run` rather than inside it:

```
FieldSnapshot
  snapshot_id, project, hosts[], window_from, window_to, fetched_at
  sessions      SessionKpis   bounce_pct, conversion_pct, avg_session_pages,
                              session_count, series[]
  vitals        FieldVitals   lcp_p75/p95, inp_p75/p95, cls_p75/p95,
                              ttfb_p75, plt_p75, series[]
  frustration   Frustration   rage_clicks_total, frustration_p75, series[]
  by_device     [DeviceRow]
  by_country    [CountryRow]
  by_pagetype   [PageTypeRow] page_group, beacons, lcp_p75, inp_p75, cls_p75,
                              plt_p75, bounce_pct, frustration_p75, rage_clicks
  inp_buckets   [InpBucketRow]
  assets        [AssetRow]    request_count, avg_size_kb, edge_ms, origin_ms,
                              cache_hit_pct
```

`by_pagetype` is the single source for both the `field.segments` page-type
table and the per-page join in §6.2 — one query, one row list, no chance of the
two disagreeing about the same page group.

**Why not fold this into `Run`.** `Run` is validated per page × device ×
network, and `_require_cwv_for_automated` enforces the lab CWV trio. Field data
has no `condition` — it is session-scoped and brand-wide — so folding it in
would make both that validator and the `runs` table describe something they do
not hold.

Every metric field is `Optional` with no default coercion, following the rule
`MainThreadMetrics` already documents: a panel returning nothing stays `None`.
A zero bounce rate and an unmeasured bounce rate must never render identically.

`window_from`/`window_to` are stored **absolute UTC**, resolved at fetch time.
`now-7d` is not reproducible; a report re-rendered next month must still state
the week it describes.

### 5. Persistence — `store/sql.py`

New table, following the existing `runs` pattern exactly — queryable columns
for lookup, full JSON for fidelity:

```sql
CREATE TABLE IF NOT EXISTS field_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    project      TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    window_from  TEXT NOT NULL,
    window_to    TEXT NOT NULL,
    hosts        TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_field_project_fetched
    ON field_snapshots (project, fetched_at);
```

With `insert_snapshot`, `get_latest_snapshot(project)` and `list_snapshots`.

New pipeline stage `ingest/field.py`, registered in `cli.py` as
**`python -m cli ingest field`**, sitting beside `ingest auto` and
`ingest manual`. It fetches, validates, persists, and prints what it stored.

Persisting rather than fetching inline buys three things: reports reproducible
offline from the store, campaign-over-campaign field history for the existing
trends module, and a CI job that never touches Grafana.

### 6. Consumption

#### 6.1 RAG — `rag/prompt.py`, `rag/retrieve.py`

Field data enters the **`# MEASUREMENTS (trusted)`** block, not the untrusted
`# CONTEXT` region. The justification is the same one that makes browser
measurements trusted: this system fetched them itself, over an authenticated
channel, from the operator's own datasource. It is not retrieved third-party
text.

String values originating in the datasource — `pageGroupName`, `countryCode`,
asset-type labels — still pass through `neutralize()` and are length-capped.
They are low-risk, but they are data this system did not author, and the cost
of defanging them is nothing.

Each page prompt receives the brand headline **plus only its own page-group
slice**, never the whole snapshot. The budget caps input at 250k tokens/day;
repeating nine panels across six page prompts would spend a large share of the
day's allowance on duplicated text.

`detect_symptoms` gains field rules, driven by new keys in the existing
`thresholds:` block rather than by numbers buried in code, exactly as every
other threshold in this project is configured:

```yaml
thresholds:
  # Field (RUM) rules. A page group's bounce rate is flagged when it exceeds
  # the site-wide rate by this many percentage points — a ratio would make a
  # low-traffic group with 2% bounce look catastrophic at 4%.
  field_bounce_excess_pp: 10.0
  # CDN cache-hit ratio below which an asset type is called out.
  field_cache_hit_warn_pct: 70.0
  # Frustration index (0-100) at which a segment or INP bucket is flagged.
  field_frustration_warn: 30.0
  field_frustration_fail: 60.0
```

Defaults match the colour thresholds already set on the dashboard panels, so
the report and the dashboard agree about what counts as bad.

These rules are **rule-based**, so a campaign degraded to the no-LLM path by an
exhausted budget still surfaces field findings instead of losing the feature
entirely.

#### 6.2 The page join

`page.field` matches a `by_pagetype` row to a lab page through the
`grafana.page_groups` mapping in §1.

This mapping is **configured, not inferred**. `pageGroupName` is an
mPulse-side taxonomy (`Home`, `Plp`, `Pdp`, `CartPage`, `Payment`, `Thankyou`)
and `targets.yaml` page names are operator-chosen (`homepage`, `plp`, `pdp`);
they coincide today by luck, not by rule. An unmapped page renders its field
row in the explicit "not available" state rather than guessing.

### 7. Report and skeleton

The project's headline promise is that the skeleton never changes. This feature
changes it **once, deliberately**, as a reviewed `report --update-baseline`
commit. Five entries added to `report/skeleton.baseline.json`:

```
  plan
+ field                  after plan — the ranked plan stays the first thing read
+ field.headline         bounce · conversion · pages/session · field CWV vs target
+ field.frustration      rage clicks · frustration p75 · INP→frustration buckets
+ field.segments         device · country · page-type tables
+ field.assets           CDN cache-hit % · edge vs origin per asset type
  page[]
  page.header
  page.at-a-glance
+ page.field             lab vs field, side by side, for this page's group
  page.findings
```

Both templates (`report.html.j2`, `report.md.j2`) gain the blocks;
`analysis/reportmodel.py` gains the corresponding models and builders.

`report/charts.py` gains two charts, deliberately not more: session KPIs over
time (bounce and conversion on a second axis, as the dashboard draws it), and a
lab-versus-field CWV comparison. The remaining panels are tables — several
dashboard panels are visual trend aids whose value collapses into text, and
rendering all of them would pad the PDF without informing the reader.

`page.field` is where the feature earns its place: the emulated mid-mobile LCP
and the p75 real users actually get, on one row.

### 8. Failure behaviour

**The `field` section always renders.** A section that disappears when Grafana
is unreachable is exactly the drift `--skeleton-check` exists to catch, so
absence is a *state*, not an omission: the section renders "No field data for
this window" and `meta.field_mode` records `live` / `stale` / `unavailable`.

- `ingest field` **exits non-zero** on fetch failure. A bad token, an
  unreachable host or a rejected query are all things a user can fix, and the
  existing convention is that those are errors.
- `analysis` **never fails** because Grafana was down or no snapshot exists,
  matching how it already treats a missing API key: it degrades and says so.
- A snapshot is `stale` when its `window_to` precedes the report's
  `generated_at` by more than one window length — a 7d snapshot fetched more
  than 7 days ago no longer overlaps the period it is being read against. It
  still renders, with its fetch date stated, so the reader can see how old the
  comparison is rather than being silently given last month's numbers.

### 9. Testing

Fixtures only — no live Grafana in CI, and no network in any test.

| Component | Test |
| --- | --- |
| `queries.py` | **No `${...}` survives rendering** — the failure that would otherwise reach production |
| `queries.py` | Hostnames failing the pattern raise; valid ones quote correctly |
| `parse.py` | Real recorded response → typed models; reordered columns still map; missing field → `None` |
| `client.py` | Bearer header set; 5xx retried once; 4xx not retried; timeout honoured |
| `store/sql.py` | Snapshot round-trips; `get_latest_snapshot` picks the newest |
| `rag/prompt.py` | Page slice contains only its own group; token bound respected; datasource strings neutralised |
| `detect_symptoms` | Field rules fire on rule-based path with no LLM |
| `report` | Skeleton matches the updated baseline; `unavailable` state renders every section |

### 10. Files

**New:** `ingest/grafana/{__init__,client,queries,parse}.py` ·
`ingest/field.py` · `normalize/field.py` · tests and the response fixture.

**Changed:** `config/load.py` · `config/settings.yaml` · `.env.example` ·
`store/sql.py` · `cli.py` · `analysis/__main__.py` · `analysis/reportmodel.py` ·
`rag/prompt.py` · `rag/retrieve.py` · `report/template/report.html.j2` ·
`report/template/report.md.j2` · `report/charts.py` ·
`report/skeleton.baseline.json` · `README.md` · `docs/PROJECT_SPEC.md`.

**Unchanged, deliberately:** `normalize/url_safety.py` and
`docs/SECURITY_PLAN.md` — Grafana is a public https host, so this feature
creates no exception to the SSRF guard.

## Open items for implementation

These are known and do not block the plan; they are resolved with operator
input during implementation:

1. **`GRAFANA_BASE_URL` is not yet known.** Config ships with an empty default;
   an empty base URL means the stage is not configured and `ingest field`
   says so rather than failing obscurely.
2. **No recorded API response yet.** `parse.py` is written against the
   documented frame format and reconciled against a real response when one is
   available. The by-name mapping in §3.4 is what makes that reconciliation
   cheap.
3. **The `page_groups` mapping ships with the three current pages.** Extending
   the campaign means extending the map; an unmapped page degrades visibly.
