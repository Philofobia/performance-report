# Grafana Field-Data Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch mPulse RUM data from Grafana over `/api/ds/query`, persist it beside the runs, feed it to the RAG layer as trusted measurement, and print it in the report both brand-wide and joined onto each tested page.

**Architecture:** A new `FieldSnapshot` entity sits beside `Run` — never inside it, because `Run` is validated per page × device × network and field data has no `condition`. A new `ingest/grafana/` package renders SQL (substituting the dashboard template variables Grafana only expands in the browser), fetches, and parses column-major frames. A new `python -m cli ingest field` stage persists snapshots to SQLite. Analysis reads the latest snapshot and threads it through prompts, symptom rules, and the report model.

**Tech Stack:** Python 3.11+, Pydantic 2, stdlib `urllib.request` (no new dependency), SQLite, Jinja2, matplotlib, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-grafana-field-ingestion-design.md`

## Global Constraints

- **No new runtime dependency.** HTTP is stdlib `urllib.request`. This repo has twice deleted unused pins (`reportlab`, `typer`) and records that it "pins what it imports".
- **Every metric field is `Optional` with no default coercion.** A panel returning nothing stays `None`, never `0` — follow the rule `MainThreadMetrics` documents in `normalize/schema.py`.
- **Secrets never appear in error messages** (SECURITY_PLAN §2.8). Follow `rag.embeddings.resolve_api_key()`.
- **`.env` holds connection identity** (`GRAFANA_BASE_URL`, `GRAFANA_TOKEN`, `GRAFANA_DATASOURCE_UID`, `GRAFANA_TABLE`); **`settings.yaml` holds query behaviour** (window, timeout, page_groups).
- **The `field` section always renders.** Absence is a state (`meta.field_mode` = `live`/`stale`/`unavailable`), never an omitted section — a vanishing section is exactly the drift `--skeleton-check` exists to catch.
- **`analysis` never fails because Grafana was down.** Only `ingest field` exits non-zero on fetch failure.
- **Datasource macros stay verbatim** in SQL (`$__timeFilter`, `$__timeInterval`); only `{table}` and `{hosts}` are substituted.
- **No network in any test.** Fixtures only.
- Run tests with `python -m pytest`. Tests live in `tests/unit/` and `tests/integration/`, named `*_test.py`.

---

### Task 1: Configuration — settings block, thresholds, env template

**Files:**
- Modify: `config/load.py` (add `GrafanaConfig`, extend `Thresholds`, mount on `Settings`)
- Modify: `config/settings.yaml`
- Modify: `.env.example`
- Test: `tests/unit/config_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `config.load.GrafanaConfig` with fields `window: str`, `timeout_s: int`, `page_groups: Dict[str, str]`; `Settings.grafana: GrafanaConfig`; `Thresholds.field_bounce_excess_pp: float`, `.field_cache_hit_warn_pct: float`, `.field_frustration_warn: float`, `.field_frustration_fail: float`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/config_test.py`:

```python
def test_grafana_defaults_present():
    from config.load import Settings

    settings = Settings()
    assert settings.grafana.window == "7d"
    assert settings.grafana.timeout_s == 30
    assert settings.grafana.page_groups == {}


def test_grafana_block_loads_from_yaml(tmp_path):
    from config.load import load_settings

    (tmp_path / "settings.yaml").write_text(
        "grafana:\n"
        "  window: 24h\n"
        "  timeout_s: 10\n"
        "  page_groups:\n"
        "    Pdp: pdp\n",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path / "settings.yaml")
    assert settings.grafana.window == "24h"
    assert settings.grafana.timeout_s == 10
    assert settings.grafana.page_groups["Pdp"] == "pdp"


def test_field_thresholds_default_to_dashboard_values():
    from config.load import Thresholds

    th = Thresholds()
    assert th.field_bounce_excess_pp == 10.0
    assert th.field_cache_hit_warn_pct == 70.0
    assert th.field_frustration_warn == 30.0
    assert th.field_frustration_fail == 60.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/config_test.py -k grafana -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'grafana'`.

- [ ] **Step 3: Write minimal implementation**

In `config/load.py`, add beside `RagConfig`:

```python
class GrafanaConfig(BaseModel):
    """How field data is queried and interpreted.

    Connection identity — base URL, token, datasource uid, table — lives in
    ``.env``, not here: it is per-instance rather than per-project, and the
    table name carries the organisation's tenant. What stays in this file is
    everything a reviewer should see change in a diff.
    """

    #: Relative window in Grafana syntax. Matches the dashboard default.
    window: str = "7d"
    timeout_s: int = Field(default=30, ge=1)
    #: mPulse ``pageGroupName`` -> ``targets.yaml`` page name. Cannot be
    #: inferred: the two vocabularies coincide today by luck, not by rule.
    #: An unmapped page renders its field row in the "not available" state.
    page_groups: Dict[str, str] = Field(default_factory=dict)
```

Extend `Thresholds` with:

```python
    # Field (RUM) rules. Defaults match the colour thresholds already set on
    # the Grafana panels, so the report and the dashboard agree about what
    # counts as bad.
    #: Percentage *points* above the site-wide rate, not a ratio: a ratio makes
    #: a low-traffic group with 2% bounce look catastrophic at 4%.
    field_bounce_excess_pp: float = 10.0
    field_cache_hit_warn_pct: float = 70.0
    field_frustration_warn: float = 30.0
    field_frustration_fail: float = 60.0
```

Mount on `Settings`:

```python
    grafana: GrafanaConfig = Field(default_factory=GrafanaConfig)
```

In `config/settings.yaml`, append the `grafana:` block and the four `thresholds:` keys exactly as written in spec §1.2 and §6.1.

In `.env.example`, append:

```bash
# --- Grafana field (RUM) ingestion — see docs/superpowers/specs/2026-08-24-* ---
# Only needed for `python -m cli ingest field`. Unset means the stage is not
# configured; analysis still runs and the report's field section says so.
#
# Grafana instance. Must be a public https URL — normalize/url_safety.py
# applies to it unchanged.
GRAFANA_BASE_URL=
# Service-account token with Viewer rights on the ClickHouse datasource.
GRAFANA_TOKEN=
# ClickHouse datasource uid, from the dashboard JSON.
GRAFANA_DATASOURCE_UID=
# Fully-qualified table the panels read, e.g. <tenant>.mpulse
GRAFANA_TABLE=
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/config_test.py -v`
Expected: PASS, including the pre-existing config tests.

- [ ] **Step 5: Commit**

```bash
git add config/load.py config/settings.yaml .env.example tests/unit/config_test.py
git commit -m "Add Grafana query config and field threshold defaults"
```

---

### Task 2: The `FieldSnapshot` model

**Files:**
- Create: `normalize/field.py`
- Test: `tests/unit/field_schema_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `normalize.field.FieldSnapshot` and sub-models `SessionKpis`, `FieldVitals`, `Frustration`, `DeviceRow`, `CountryRow`, `PageTypeRow`, `InpBucketRow`, `AssetRow`, `TimePoint`. `FieldSnapshot.page_row(group: str) -> Optional[PageTypeRow]`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/field_schema_test.py`:

```python
"""FieldSnapshot validation — the canonical field-data object."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from normalize.field import FieldSnapshot, PageTypeRow, SessionKpis


def _snapshot(**kwargs) -> FieldSnapshot:
    base = dict(
        snapshot_id="oakley-20260824T100000Z",
        project="oakley",
        hosts=["www.oakley.com"],
        window_from=datetime(2026, 8, 17, tzinfo=timezone.utc),
        window_to=datetime(2026, 8, 24, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    base.update(kwargs)
    return FieldSnapshot(**base)


def test_minimal_snapshot_validates_with_every_metric_none():
    snap = _snapshot()
    assert snap.sessions.bounce_pct is None
    assert snap.vitals.lcp_p75 is None
    assert snap.by_device == []


def test_unmeasured_is_not_zero():
    """A panel that returned nothing must not read as a measured zero."""
    snap = _snapshot()
    assert snap.sessions.bounce_pct is None
    assert snap.sessions.session_count is None
    # The distinction the whole model turns on: None formats as an em dash,
    # 0 formats as a number a reader would act on.
    assert snap.sessions.bounce_pct != 0


def test_percentages_are_bounded():
    with pytest.raises(ValidationError):
        _snapshot(sessions=SessionKpis(bounce_pct=101.0))
    with pytest.raises(ValidationError):
        _snapshot(sessions=SessionKpis(conversion_pct=-1.0))


def test_window_must_not_be_inverted():
    with pytest.raises(ValidationError):
        _snapshot(
            window_from=datetime(2026, 8, 24, tzinfo=timezone.utc),
            window_to=datetime(2026, 8, 17, tzinfo=timezone.utc),
        )


def test_page_row_looks_up_by_group():
    snap = _snapshot(by_pagetype=[
        PageTypeRow(page_group="Pdp", beacons=900, lcp_p75=4100.0),
        PageTypeRow(page_group="Home", beacons=500, lcp_p75=2200.0),
    ])
    assert snap.page_row("Pdp").lcp_p75 == 4100.0
    assert snap.page_row("Payment") is None


def test_hosts_must_not_be_empty():
    with pytest.raises(ValidationError):
        _snapshot(hosts=[])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/field_schema_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'normalize.field'`.

- [ ] **Step 3: Write minimal implementation**

Create `normalize/field.py`:

```python
"""Pydantic canonical field-data object (design 2026-08-24 §4).

``FieldSnapshot`` sits *beside* :class:`normalize.schema.Run`, never inside it.
``Run`` is validated per page x device x network and ``_require_cwv_for_automated``
enforces a lab CWV trio; field data is session-scoped and brand-wide with no
``condition`` at all, so folding it in would make both that validator and the
``runs`` table describe something they do not hold.

Every metric is ``Optional`` with no default coercion, following the rule
``MainThreadMetrics`` documents: a panel that returned nothing stays ``None``.
A zero bounce rate and an unmeasured bounce rate must never render identically.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

#: Percentages arriving from ClickHouse are already 0..100.
Pct = Optional[float]


class TimePoint(BaseModel):
    """One bucket of a time series. ``values`` is keyed by column name."""

    time: datetime
    values: Dict[str, Optional[float]] = Field(default_factory=dict)


class SessionKpis(BaseModel):
    """Whole-window session aggregates, plus the series behind them."""

    bounce_pct: Pct = Field(default=None, ge=0, le=100)
    conversion_pct: Pct = Field(default=None, ge=0, le=100)
    avg_session_pages: Optional[float] = Field(default=None, ge=0)
    session_count: Optional[int] = Field(default=None, ge=0)
    series: List[TimePoint] = Field(default_factory=list)


class FieldVitals(BaseModel):
    """Real-user Core Web Vitals. Compare against the lab numbers, not to them."""

    lcp_p75: Optional[float] = Field(default=None, ge=0)
    lcp_p95: Optional[float] = Field(default=None, ge=0)
    inp_p75: Optional[float] = Field(default=None, ge=0)
    inp_p95: Optional[float] = Field(default=None, ge=0)
    #: Already divided by 1000 at query time — mPulse stores CLS x1000.
    cls_p75: Optional[float] = Field(default=None, ge=0)
    cls_p95: Optional[float] = Field(default=None, ge=0)
    ttfb_p75: Optional[float] = Field(default=None, ge=0)
    plt_p75: Optional[float] = Field(default=None, ge=0)
    series: List[TimePoint] = Field(default_factory=list)


class Frustration(BaseModel):
    rage_clicks_total: Optional[int] = Field(default=None, ge=0)
    frustration_p75: Optional[float] = Field(default=None, ge=0, le=100)
    series: List[TimePoint] = Field(default_factory=list)


class _SegmentRow(BaseModel):
    """Columns every breakdown row shares."""

    beacons: Optional[int] = Field(default=None, ge=0)
    lcp_p75: Optional[float] = Field(default=None, ge=0)
    inp_p75: Optional[float] = Field(default=None, ge=0)
    cls_p75: Optional[float] = Field(default=None, ge=0)
    plt_p75: Optional[float] = Field(default=None, ge=0)
    ttfb_p75: Optional[float] = Field(default=None, ge=0)
    frustration_p75: Optional[float] = Field(default=None, ge=0, le=100)
    rage_clicks: Optional[int] = Field(default=None, ge=0)


class DeviceRow(_SegmentRow):
    device: str = Field(min_length=1)
    rage_pct: Pct = Field(default=None, ge=0, le=100)
    avg_session_pages: Optional[float] = Field(default=None, ge=0)


class CountryRow(_SegmentRow):
    country: str = Field(min_length=1)


class PageTypeRow(_SegmentRow):
    page_group: str = Field(min_length=1)
    #: Sessions that *entered* on this page group and viewed one page. Not the
    #: same quantity as the site-wide bounce rate, which is per session
    #: regardless of entry point - see the query in ingest/grafana/queries.py.
    bounce_pct: Pct = Field(default=None, ge=0, le=100)


class InpBucketRow(BaseModel):
    bucket: str = Field(min_length=1)
    beacons: Optional[int] = Field(default=None, ge=0)
    avg_frustration: Optional[float] = Field(default=None, ge=0, le=100)
    rage_session_pct: Pct = Field(default=None, ge=0, le=100)


class AssetRow(BaseModel):
    asset_type: str = Field(min_length=1)
    request_count: Optional[int] = Field(default=None, ge=0)
    avg_size_kb: Optional[float] = Field(default=None, ge=0)
    edge_ms: Optional[float] = Field(default=None, ge=0)
    origin_ms: Optional[float] = Field(default=None, ge=0)
    cache_hit_pct: Pct = Field(default=None, ge=0, le=100)


class FieldSnapshot(BaseModel):
    """One fetch of field data for a project, over one absolute window."""

    snapshot_id: str = Field(min_length=1)
    project: str = Field(min_length=1)
    hosts: List[str] = Field(min_length=1)
    #: Absolute UTC, resolved at fetch time. "now-7d" is not reproducible, and
    #: a report re-rendered next month must still state the week it describes.
    window_from: datetime
    window_to: datetime
    fetched_at: datetime

    sessions: SessionKpis = Field(default_factory=SessionKpis)
    vitals: FieldVitals = Field(default_factory=FieldVitals)
    frustration: Frustration = Field(default_factory=Frustration)
    by_device: List[DeviceRow] = Field(default_factory=list)
    by_country: List[CountryRow] = Field(default_factory=list)
    by_pagetype: List[PageTypeRow] = Field(default_factory=list)
    inp_buckets: List[InpBucketRow] = Field(default_factory=list)
    assets: List[AssetRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _window_ordered(self) -> "FieldSnapshot":
        if self.window_to < self.window_from:
            raise ValueError("window_to must not precede window_from")
        return self

    def page_row(self, group: str) -> Optional[PageTypeRow]:
        """The breakdown row for one ``pageGroupName``, or None if absent."""
        for row in self.by_pagetype:
            if row.page_group == group:
                return row
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/field_schema_test.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add normalize/field.py tests/unit/field_schema_test.py
git commit -m "Add the FieldSnapshot canonical model"
```

---

### Task 3: SQL templates and host validation

**Files:**
- Create: `ingest/grafana/__init__.py`, `ingest/grafana/queries.py`
- Test: `tests/unit/grafana_queries_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ingest.grafana.queries.QUERIES: Dict[str, str]` (8 entries keyed by refId), `quote_hosts(hosts: Sequence[str]) -> str`, `render(ref_id: str, *, table: str, hosts: Sequence[str]) -> str`, `render_all(*, table, hosts) -> Dict[str, str]`, `HostError(ValueError)`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/grafana_queries_test.py`:

```python
"""SQL rendering — the injection surface and the variable-substitution trap."""
import re

import pytest

from ingest.grafana.queries import (
    QUERIES,
    HostError,
    quote_hosts,
    render,
    render_all,
)

TABLE = "tenant.mpulse"
HOSTS = ["www.oakley.com", "m.oakley.com"]


def test_all_eight_panels_are_defined():
    assert set(QUERIES) == {
        "sessions", "vitals", "frustration", "by_device",
        "by_country", "by_pagetype", "inp_buckets", "assets",
    }


def test_no_dashboard_variable_survives_rendering():
    """Grafana interpolates ${...} in the browser; an API caller must not
    leave one in the SQL or ClickHouse receives it as literal text."""
    for ref_id, sql in render_all(table=TABLE, hosts=HOSTS).items():
        assert "${" not in sql, f"{ref_id} still carries a dashboard variable"
        assert "{table}" not in sql and "{hosts}" not in sql, ref_id


def test_datasource_macros_are_left_verbatim():
    """$__timeFilter and $__timeInterval are expanded by the plugin, not us."""
    sql = render("vitals", table=TABLE, hosts=HOSTS)
    assert "$__timeFilter(timestamp)" in sql
    assert "$__timeInterval(timestamp)" in sql


def test_table_and_hosts_are_substituted():
    sql = render("vitals", table=TABLE, hosts=HOSTS)
    assert TABLE in sql
    assert "'www.oakley.com'" in sql and "'m.oakley.com'" in sql


def test_quote_hosts_renders_a_sql_list():
    assert quote_hosts(["a.com", "b.com"]) == "'a.com', 'b.com'"


@pytest.mark.parametrize("bad", [
    "www.oakley.com'; DROP TABLE mpulse--",
    "oakley.com OR 1=1",
    "-leading-dash.com",
    "UPPER.COM",
    "has space.com",
    "",
])
def test_hostile_hostnames_are_rejected_not_escaped(bad):
    """targets.yaml is operator-authored: a quote in a hostname is a mistake
    or an attack, and neither should reach ClickHouse."""
    with pytest.raises(HostError):
        quote_hosts([bad])


def test_empty_host_list_is_rejected():
    with pytest.raises(HostError):
        quote_hosts([])


def test_unknown_ref_id_is_rejected():
    with pytest.raises(KeyError):
        render("not_a_panel", table=TABLE, hosts=HOSTS)


def test_by_pagetype_selects_the_columns_the_join_needs():
    sql = render("by_pagetype", table=TABLE, hosts=HOSTS)
    for column in ("page_group", "lcp_p75", "inp_p75", "frustration_p75",
                   "rage_clicks", "bounce_pct"):
        assert re.search(rf"\b{column}\b", sql), column
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/grafana_queries_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest.grafana'`.

- [ ] **Step 3: Write minimal implementation**

Create `ingest/grafana/__init__.py`:

```python
"""Grafana field-data ingestion: render SQL, fetch frames, parse to models."""
```

Create `ingest/grafana/queries.py`:

```python
"""The eight panel queries, and the host validation that keeps them safe.

Grafana expands **datasource macros** (``$__timeFilter``, ``$__timeInterval``)
server-side in the ClickHouse plugin, so those are left verbatim. It expands
**dashboard template variables** (``${brand:sqlstring}``, ``${mpulse}``) in the
*browser*, before dispatch — an API caller gets no such treatment, so this
module owns that substitution.

Owning it means building SQL by string substitution against a datasource whose
token carries broad read rights. ``quote_hosts`` is therefore an allowlist that
*rejects* rather than escapes: ``config/targets.yaml`` is operator-authored, so
a hostname containing a quote is a mistake or an attack, and neither has any
business reaching ClickHouse.
"""
from __future__ import annotations

import re
from typing import Dict, Sequence

#: Deliberately strict. Lowercase because ``pageDomainName`` is stored
#: lowercase; no underscores because they are not legal in hostnames.
_HOSTNAME = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


class HostError(ValueError):
    """A hostname that must not be interpolated into SQL."""


def quote_hosts(hosts: Sequence[str]) -> str:
    """Render a validated ``IN (...)`` list body, or raise.

    The hostname never reaches the quoting step unless it matched the
    allowlist, so there is no escaping path to get wrong.
    """
    cleaned = [h.strip() for h in hosts]
    if not cleaned:
        raise HostError(
            "No hosts to query. config/targets.yaml must list at least one "
            "page whose URL has a hostname."
        )
    for host in cleaned:
        if not _HOSTNAME.match(host):
            raise HostError(
                f"Refusing to build SQL with hostname {host!r}: it is not a "
                "plain lowercase hostname."
            )
    return ", ".join(f"'{host}'" for host in cleaned)


# Shared fragments -----------------------------------------------------------
_LCP = "largestContentfulPaint > 0 AND largestContentfulPaint < 60000"
_INP = "interactionToNextPaint > 0 AND interactionToNextPaint < 10000"
_CLS = "cumulativeLayoutShift >= 0 AND cumulativeLayoutShift < 100000"
_PLT = "pageLoadTime > 0 AND pageLoadTime < 60000"
_TTFB = "firstByteTimer > 0 AND firstByteTimer < 30000"
_FRU = "frustrationIndex > 0 AND frustrationIndex <= 100"
_WHERE = "$__timeFilter(timestamp) AND pageDomainName IN ({hosts})"
#: A session is a page-view beacon carrying an id under either key.
_SESSION_FILTER = (
    "((sessionId IS NOT NULL) OR (paramsRtSi IS NOT NULL)) "
    "AND beaconTypeName IN ('page view', 'spa_hard')"
)

_CWV_COLUMNS = f"""\
    round(quantileIf(0.75)(largestContentfulPaint, {_LCP}), 0) AS lcp_p75,
    round(quantileIf(0.75)(interactionToNextPaint, {_INP}), 0) AS inp_p75,
    round(quantileIf(0.75)(cumulativeLayoutShift, {_CLS}) / 1000.0, 3) AS cls_p75,
    round(quantileIf(0.75)(pageLoadTime, {_PLT}), 0) AS plt_p75,
    round(quantileIf(0.75)(firstByteTimer, {_TTFB}), 0) AS ttfb_p75,
    round(quantileIf(0.75)(frustrationIndex, {_FRU}), 1) AS frustration_p75,
    sum(rageClicks) AS rage_clicks"""


QUERIES: Dict[str, str] = {
    # Session-level KPIs. Grouped by session first, then bucketed by the
    # session's *first* beacon, so a long session counts once.
    "sessions": f"""
WITH raw AS (
    SELECT COALESCE(sessionId, paramsRtSi) AS sid,
           sessionPages,
           CASE WHEN pageGroupName = 'Thankyou' THEN 1 ELSE 0 END AS converted,
           timestamp,
           $__timeInterval(timestamp) AS time
    FROM {{table}}
    WHERE {_WHERE} AND {_SESSION_FILTER}
), base AS (
    SELECT sid,
           MIN(time) AS time,
           argMax(sessionPages, timestamp) AS pages,
           MAX(converted) AS converted
    FROM raw GROUP BY sid
)
SELECT time,
       COUNT(*) AS session_count,
       AVG(pages) AS avg_session_pages,
       (countIf(pages = 1) / COUNT(*)) * 100 AS bounce_pct,
       (countIf(converted = 1) / COUNT(*)) * 100 AS conversion_pct
FROM base GROUP BY time ORDER BY time ASC""",

    "vitals": f"""
SELECT $__timeInterval(timestamp) AS time,
       quantileIf(0.75)(largestContentfulPaint, {_LCP}) AS lcp_p75,
       quantileIf(0.95)(largestContentfulPaint, {_LCP}) AS lcp_p95,
       quantileIf(0.75)(interactionToNextPaint, {_INP}) AS inp_p75,
       quantileIf(0.95)(interactionToNextPaint, {_INP}) AS inp_p95,
       quantileIf(0.75)(cumulativeLayoutShift, {_CLS}) / 1000.0 AS cls_p75,
       quantileIf(0.95)(cumulativeLayoutShift, {_CLS}) / 1000.0 AS cls_p95,
       quantileIf(0.75)(firstByteTimer, {_TTFB}) AS ttfb_p75,
       quantileIf(0.75)(pageLoadTime, {_PLT}) AS plt_p75
FROM {{table}}
WHERE {_WHERE}
GROUP BY time ORDER BY time""",

    "frustration": f"""
SELECT $__timeInterval(timestamp) AS time,
       sum(rageClicks) AS rage_clicks,
       quantileIf(0.75)(frustrationIndex, {_FRU}) AS frustration_p75
FROM {{table}}
WHERE {_WHERE}
GROUP BY time ORDER BY time""",

    # Merges the dashboard's two per-device panels: they differ only in which
    # columns they select from the same GROUP BY.
    "by_device": f"""
SELECT deviceTypeName AS device,
       count() AS beacons,
{_CWV_COLUMNS},
       round(100.0 * countIf(rageClicks > 0) / count(), 2) AS rage_pct,
       round(avgIf(sessionPages, sessionPages > 0 AND sessionPages < 100), 2)
           AS avg_session_pages
FROM {{table}}
WHERE {_WHERE} AND deviceTypeName != ''
GROUP BY deviceTypeName ORDER BY beacons DESC""",

    "by_country": f"""
SELECT countryCode AS country,
       count() AS beacons,
{_CWV_COLUMNS}
FROM {{table}}
WHERE {_WHERE} AND countryCode != ''
GROUP BY countryCode HAVING beacons > 50 ORDER BY beacons DESC LIMIT 15""",

    # CWV per page group LEFT JOINed to entry-page bounce. One query, one row
    # list, so the segments table and the per-page join cannot disagree.
    # NOTE: bounce here is *entry-page* bounce - sessions that started on this
    # group and viewed one page. Not the same quantity as site-wide bounce.
    "by_pagetype": f"""
SELECT b.page_group AS page_group,
       b.beacons AS beacons,
       b.lcp_p75 AS lcp_p75,
       b.inp_p75 AS inp_p75,
       b.cls_p75 AS cls_p75,
       b.plt_p75 AS plt_p75,
       b.ttfb_p75 AS ttfb_p75,
       b.frustration_p75 AS frustration_p75,
       b.rage_clicks AS rage_clicks,
       s.bounce_pct AS bounce_pct
FROM (
    SELECT pageGroupName AS page_group,
           count() AS beacons,
{_CWV_COLUMNS}
    FROM {{table}}
    WHERE {_WHERE} AND pageGroupName != ''
    GROUP BY pageGroupName HAVING beacons > 50
) AS b
LEFT JOIN (
    SELECT entry_group AS page_group,
           (countIf(pages = 1) / COUNT(*)) * 100 AS bounce_pct
    FROM (
        SELECT COALESCE(sessionId, paramsRtSi) AS sid,
               argMin(pageGroupName, timestamp) AS entry_group,
               argMax(sessionPages, timestamp) AS pages
        FROM {{table}}
        WHERE {_WHERE} AND {_SESSION_FILTER} AND pageGroupName != ''
        GROUP BY sid
    )
    GROUP BY entry_group
) AS s ON b.page_group = s.page_group
ORDER BY beacons DESC""",

    "inp_buckets": f"""
SELECT inp_bucket AS bucket,
       count() AS beacons,
       round(avg(frustration), 1) AS avg_frustration,
       round(100.0 * countIf(rage > 0) / count(), 2) AS rage_session_pct
FROM (
    SELECT multiIf(interactionToNextPaint < 200, '1 - under 200 ms (good)',
                   interactionToNextPaint < 500, '2 - 200-500 ms (needs work)',
                   interactionToNextPaint < 1000, '3 - 500-1000 ms (poor)',
                   '4 - over 1000 ms (critical)') AS inp_bucket,
           frustrationIndex AS frustration,
           rageClicks AS rage
    FROM {{table}}
    WHERE {_WHERE} AND {_INP} AND {_FRU}
)
GROUP BY inp_bucket HAVING beacons > 50 ORDER BY inp_bucket""",

    "assets": _assets_sql(),
}
```

`_assets_sql()` is defined **above** `QUERIES` (Python evaluates the dict literal at import, so the helper must already exist). It builds the six-way `UNION ALL` rather than writing it out six times:

```python
#: (label, column prefix) for the asset-type breakdown. The mPulse schema
#: names every column with the same prefix, so the six branches of the UNION
#: differ only by that prefix - generating them keeps one definition of the
#: arithmetic instead of six copies drifting apart.
_ASSET_TYPES = (
    ("HTML", "html"), ("CSS", "css"), ("JavaScript", "js"),
    ("Images", "img"), ("Fonts", "font"), ("XHR/API", "xhr"),
)


def _assets_sql() -> str:
    branches = []
    for label, prefix in _ASSET_TYPES:
        branches.append(f"""\
    SELECT '{label}' AS asset_type,
           sum({prefix}RequestCount) AS request_count,
           round(avg({prefix}TransferSize) / 1024.0, 1) AS avg_size_kb,
           round(avg({prefix}CdnEdgeTime), 0) AS edge_ms,
           round(avg({prefix}OriginTime), 0) AS origin_ms,
           round(least(avg({prefix}CdnCacheHitRatio) * 100.0, 100), 1)
               AS cache_hit_pct
    FROM {{table}}
    WHERE {_WHERE} AND {prefix}RequestCount > 0""")
    return (
        "\nSELECT * FROM (\n"
        + "\n    UNION ALL\n".join(branches)
        + "\n) ORDER BY request_count DESC"
    )
```

and set `"assets": _assets_sql(),` in `QUERIES`.

Finally the render functions:

```python
def render(ref_id: str, *, table: str, hosts: Sequence[str]) -> str:
    """One panel's SQL, with the dashboard variables substituted.

    ``KeyError`` on an unknown refId rather than a silent empty string: a
    typo'd panel name should fail here, not produce a query that returns
    nothing and reads as "no field data".
    """
    template = QUERIES[ref_id]
    return template.format(table=table, hosts=quote_hosts(hosts))


def render_all(*, table: str, hosts: Sequence[str]) -> Dict[str, str]:
    """Every panel's SQL, keyed by refId."""
    return {
        ref_id: render(ref_id, table=table, hosts=hosts) for ref_id in QUERIES
    }
```

> **Implementation note:** `_WHERE` and the shared fragments contain `{hosts}`, and `_assets_sql` contains `{table}`, so they survive the f-strings that build `QUERIES` and are consumed by `.format()` in `render`. Where an f-string must emit a literal brace for `.format()` to see later, it is written `{{table}}`. Verify with the `test_no_dashboard_variable_survives_rendering` test — it fails loudly if a brace escapes wrongly.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/grafana_queries_test.py -v`
Expected: PASS (15 tests including the parametrised hostile hostnames).

- [ ] **Step 5: Commit**

```bash
git add ingest/grafana/__init__.py ingest/grafana/queries.py tests/unit/grafana_queries_test.py
git commit -m "Render the eight panel queries with validated host substitution"
```

---

### Task 4: The HTTP client

**Files:**
- Create: `ingest/grafana/client.py`
- Test: `tests/unit/grafana_client_test.py`

**Interfaces:**
- Consumes: `ingest.grafana.queries.render_all`.
- Produces: `GrafanaEnv` (dataclass: `base_url`, `token`, `datasource_uid`, `table`), `resolve_grafana_env(env=None) -> GrafanaEnv`, `GrafanaError(Exception)`, `MissingGrafanaConfigError(GrafanaError)`, `GrafanaClient(env, *, timeout_s=30, opener=None)` with `.query(sql_by_ref: Dict[str, str], *, window: str) -> Dict[str, Any]`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/grafana_client_test.py`:

```python
"""Env resolution, request shape, and retry policy. No network."""
import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from ingest.grafana.client import (
    GrafanaClient,
    GrafanaEnv,
    GrafanaError,
    MissingGrafanaConfigError,
    resolve_grafana_env,
)

FULL_ENV = {
    "GRAFANA_BASE_URL": "https://grafana.example.com",
    "GRAFANA_TOKEN": "glsa_secret_value",
    "GRAFANA_DATASOURCE_UID": "abc123",
    "GRAFANA_TABLE": "tenant.mpulse",
}


def test_resolve_reads_all_four_variables():
    env = resolve_grafana_env(FULL_ENV)
    assert env.base_url == "https://grafana.example.com"
    assert env.datasource_uid == "abc123"
    assert env.table == "tenant.mpulse"


@pytest.mark.parametrize("missing", sorted(FULL_ENV))
def test_each_missing_variable_is_named(missing):
    partial = {k: v for k, v in FULL_ENV.items() if k != missing}
    with pytest.raises(MissingGrafanaConfigError) as exc:
        resolve_grafana_env(partial)
    assert missing in str(exc.value)


def test_token_never_appears_in_an_error_message():
    """SECURITY_PLAN 2.8: the value is never echoed, only the variable name."""
    broken = dict(FULL_ENV, GRAFANA_BASE_URL="")
    with pytest.raises(MissingGrafanaConfigError) as exc:
        resolve_grafana_env(broken)
    assert "glsa_secret_value" not in str(exc.value)


def test_trailing_slash_on_base_url_is_normalised():
    env = resolve_grafana_env(dict(FULL_ENV, GRAFANA_BASE_URL="https://g.example.com/"))
    assert env.base_url == "https://g.example.com"


def test_non_https_base_url_is_rejected():
    with pytest.raises(GrafanaError):
        resolve_grafana_env(dict(FULL_ENV, GRAFANA_BASE_URL="http://g.example.com"))


class _FakeOpener:
    """Records requests and replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return BytesIO(json.dumps(outcome).encode("utf-8"))


def _client(responses):
    return GrafanaClient(
        resolve_grafana_env(FULL_ENV), timeout_s=5, opener=_FakeOpener(responses)
    )


def test_query_posts_bearer_token_and_correct_path():
    client = _client([{"results": {}}])
    client.query({"vitals": "SELECT 1"}, window="7d")
    request = client._opener.requests[0]
    assert request.full_url == "https://grafana.example.com/api/ds/query"
    assert request.get_header("Authorization") == "Bearer glsa_secret_value"
    assert request.get_method() == "POST"


def test_query_body_carries_one_entry_per_ref_id():
    client = _client([{"results": {}}])
    client.query({"vitals": "SELECT 1", "assets": "SELECT 2"}, window="24h")
    body = json.loads(client._opener.requests[0].data.decode("utf-8"))
    assert body["from"] == "now-24h" and body["to"] == "now"
    assert {q["refId"] for q in body["queries"]} == {"vitals", "assets"}
    assert body["queries"][0]["datasource"]["uid"] == "abc123"


def test_server_error_is_retried_once_then_succeeds():
    boom = HTTPError("u", 503, "unavailable", {}, None)
    client = _client([boom, {"results": {"ok": {}}}])
    assert client.query({"vitals": "SELECT 1"}, window="7d") == {"ok": {}}
    assert len(client._opener.requests) == 2


def test_client_error_is_not_retried():
    boom = HTTPError("u", 401, "unauthorized", {}, None)
    client = _client([boom])
    with pytest.raises(GrafanaError) as exc:
        client.query({"vitals": "SELECT 1"}, window="7d")
    assert "401" in str(exc.value)
    assert len(client._opener.requests) == 1


def test_connection_failure_is_retried_once_then_raises():
    client = _client([URLError("down"), URLError("down")])
    with pytest.raises(GrafanaError):
        client.query({"vitals": "SELECT 1"}, window="7d")
    assert len(client._opener.requests) == 2


def test_malformed_json_response_raises_grafana_error():
    class _BadOpener:
        requests = []

        def open(self, request, timeout=None):
            self.requests.append(request)
            return BytesIO(b"<html>gateway</html>")

    client = GrafanaClient(
        resolve_grafana_env(FULL_ENV), timeout_s=5, opener=_BadOpener()
    )
    with pytest.raises(GrafanaError):
        client.query({"vitals": "SELECT 1"}, window="7d")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/grafana_client_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest.grafana.client'`.

- [ ] **Step 3: Write minimal implementation**

Create `ingest/grafana/client.py`:

```python
"""Authenticated POST to Grafana's ``/api/ds/query``.

Transport is stdlib ``urllib.request``. This repo has twice deleted pinned
dependencies nothing imported (``reportlab``, ``typer``) and records that it
"pins what it imports"; a JSON POST with a bearer header does not justify
reversing that.

Connection identity comes from ``.env`` via :func:`resolve_grafana_env`, which
follows ``rag.embeddings.resolve_api_key``: a missing value raises a message
naming the *variable*, never echoing a value (SECURITY_PLAN 2.8).
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional
from urllib.error import HTTPError, URLError

from normalize.url_safety import UnSafeURLError, validate_url

DATASOURCE_TYPE = "grafana-clickhouse-datasource"
QUERY_PATH = "/api/ds/query"

#: Every variable this stage needs, with the one-line help its absence prints.
_REQUIRED = {
    "GRAFANA_BASE_URL": "the https URL of your Grafana instance",
    "GRAFANA_TOKEN": "a service-account token with Viewer rights on the datasource",
    "GRAFANA_DATASOURCE_UID": "the ClickHouse datasource uid from the dashboard JSON",
    "GRAFANA_TABLE": "the fully-qualified table the panels read",
}


class GrafanaError(Exception):
    """User-facing error talking to Grafana."""


class MissingGrafanaConfigError(GrafanaError):
    """A required ``.env`` variable is absent or blank."""


@dataclass(frozen=True)
class GrafanaEnv:
    base_url: str
    token: str
    datasource_uid: str
    table: str


def resolve_grafana_env(env: Optional[Mapping[str, str]] = None) -> GrafanaEnv:
    """Read the four connection variables, or explain which one is missing."""
    environ = os.environ if env is None else env
    values: Dict[str, str] = {}
    for name, help_text in _REQUIRED.items():
        value = (environ.get(name) or "").strip()
        if not value:
            raise MissingGrafanaConfigError(
                f"{name} is not set. Copy .env.example to .env and add "
                f"{help_text}. It is read from the environment and must never "
                "be committed."
            )
        values[name] = value

    base_url = values["GRAFANA_BASE_URL"].rstrip("/")
    # The same SSRF guard the browser layer uses, unchanged: https only, no
    # raw IPs, no userinfo, no private ranges. Grafana is a public host, so
    # this feature creates no exception to SECURITY_PLAN 2.2.
    try:
        validate_url(base_url)
    except UnSafeURLError as exc:
        raise GrafanaError(f"GRAFANA_BASE_URL is not usable: {exc}") from exc

    return GrafanaEnv(
        base_url=base_url,
        token=values["GRAFANA_TOKEN"],
        datasource_uid=values["GRAFANA_DATASOURCE_UID"],
        table=values["GRAFANA_TABLE"],
    )


class GrafanaClient:
    """One POST carrying every panel query, so a fetch is a single round trip."""

    def __init__(self, env: GrafanaEnv, *, timeout_s: int = 30,
                 opener: Any = None) -> None:
        self._env = env
        self._timeout = timeout_s
        self._opener = opener or urllib.request.build_opener()

    def query(self, sql_by_ref: Dict[str, str], *, window: str) -> Dict[str, Any]:
        """Execute every query; return the ``results`` map keyed by refId."""
        payload = {
            "from": f"now-{window}",
            "to": "now",
            "queries": [
                {
                    "refId": ref_id,
                    "datasource": {
                        "type": DATASOURCE_TYPE,
                        "uid": self._env.datasource_uid,
                    },
                    "rawSql": sql,
                    "format": 1,
                }
                for ref_id, sql in sorted(sql_by_ref.items())
            ],
        }
        raw = self._post(json.dumps(payload).encode("utf-8"))
        try:
            document = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GrafanaError(
                "Grafana returned a response that is not JSON. This is usually "
                "a proxy or login page in front of the API rather than Grafana "
                "itself."
            ) from exc
        return document.get("results", {})

    def _post(self, body: bytes) -> bytes:
        """POST with one retry on 5xx and on connection failure.

        A 4xx is never retried: a 401 is a bad token and a 400 is a bad query,
        and retrying either only delays the message that fixes it.
        """
        request = urllib.request.Request(
            self._env.base_url + QUERY_PATH,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._env.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    return response.read()
            except HTTPError as exc:
                if exc.code < 500:
                    raise GrafanaError(
                        f"Grafana refused the request ({exc.code} {exc.reason})."
                        + (" Check GRAFANA_TOKEN and its datasource permissions."
                           if exc.code in (401, 403) else "")
                    ) from exc
                last = exc
            except URLError as exc:
                last = exc
        raise GrafanaError(
            f"Could not reach Grafana at {self._env.base_url} after 2 attempts: "
            f"{last}"
        )
```

> **Note on `with ... as response`:** the fake opener in the test returns a `BytesIO`, which is a context manager, so the production code path and the test path agree.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/grafana_client_test.py -v`
Expected: PASS (14 tests including the four parametrised missing-variable cases).

- [ ] **Step 5: Commit**

```bash
git add ingest/grafana/client.py tests/unit/grafana_client_test.py
git commit -m "Add the Grafana query client with env resolution and retry policy"
```

---

### Task 5: Frame parsing

**Files:**
- Create: `ingest/grafana/parse.py`, `tests/fixtures/__init__.py`, `tests/fixtures/grafana_response.json`
- Test: `tests/unit/grafana_parse_test.py`

**Interfaces:**
- Consumes: `normalize.field.*`.
- Produces: `ingest.grafana.parse.frame_rows(result: Any) -> List[Dict[str, Any]]`, `build_snapshot(results, *, project, hosts, window_from, window_to, fetched_at) -> FieldSnapshot`, `ParseError(Exception)`.

- [ ] **Step 1: Write the failing test**

Create `tests/fixtures/__init__.py` (empty file), then `tests/fixtures/grafana_response.json` — a hand-built response in Grafana's real column-major shape, covering one timeseries and one table panel plus one empty panel:

```json
{
  "results": {
    "vitals": {
      "frames": [
        {
          "schema": {
            "fields": [
              {"name": "time", "type": "time"},
              {"name": "lcp_p75", "type": "number"},
              {"name": "inp_p75", "type": "number"},
              {"name": "cls_p75", "type": "number"}
            ]
          },
          "data": {
            "values": [
              [1755993600000, 1756080000000],
              [3900.0, 4100.0],
              [240.0, 260.0],
              [0.12, 0.14]
            ]
          }
        }
      ]
    },
    "by_pagetype": {
      "frames": [
        {
          "schema": {
            "fields": [
              {"name": "page_group", "type": "string"},
              {"name": "beacons", "type": "number"},
              {"name": "lcp_p75", "type": "number"},
              {"name": "frustration_p75", "type": "number"},
              {"name": "rage_clicks", "type": "number"},
              {"name": "bounce_pct", "type": "number"}
            ]
          },
          "data": {
            "values": [
              ["Pdp", "Home"],
              [1200, 800],
              [4100.0, 2200.0],
              [42.0, 18.0],
              [310, 40],
              [61.5, 38.0]
            ]
          }
        }
      ]
    },
    "assets": {"frames": []}
  }
}
```

Create `tests/unit/grafana_parse_test.py`:

```python
"""Frames to models. Mapping is by field NAME, never by position."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingest.grafana.parse import ParseError, build_snapshot, frame_rows

FIXTURE = Path(__file__).parent.parent / "fixtures" / "grafana_response.json"


def _results():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["results"]


def _snapshot(results=None):
    return build_snapshot(
        results if results is not None else _results(),
        project="oakley",
        hosts=["www.oakley.com"],
        window_from=datetime(2026, 8, 17, tzinfo=timezone.utc),
        window_to=datetime(2026, 8, 24, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )


def test_frame_rows_transposes_column_major_values():
    rows = frame_rows(_results()["by_pagetype"])
    assert rows[0] == {
        "page_group": "Pdp", "beacons": 1200, "lcp_p75": 4100.0,
        "frustration_p75": 42.0, "rage_clicks": 310, "bounce_pct": 61.5,
    }


def test_empty_frames_yield_no_rows():
    assert frame_rows(_results()["assets"]) == []


def test_missing_panel_yields_no_rows():
    assert frame_rows(None) == []


def test_columns_are_matched_by_name_not_position():
    """A column added to a panel must not shift every value one to the left."""
    results = _results()
    frame = results["by_pagetype"]["frames"][0]
    frame["schema"]["fields"].insert(
        1, {"name": "unexpected_new_column", "type": "string"}
    )
    frame["data"]["values"].insert(1, ["x", "y"])

    snap = _snapshot(results)
    assert snap.page_row("Pdp").lcp_p75 == 4100.0
    assert snap.page_row("Pdp").beacons == 1200


def test_absent_column_becomes_none_not_zero():
    snap = _snapshot()
    assert snap.page_row("Pdp").inp_p75 is None


def test_timeseries_becomes_series_and_latest_scalar():
    snap = _snapshot()
    assert len(snap.vitals.series) == 2
    # The scalar is the most recent bucket, matching the dashboard's
    # "lastNotNull" reducer.
    assert snap.vitals.lcp_p75 == 4100.0
    assert snap.vitals.series[0].time.year == 2026


def test_pagetype_rows_populate_the_join_lookup():
    snap = _snapshot()
    assert snap.page_row("Pdp").bounce_pct == 61.5
    assert snap.page_row("Home").rage_clicks == 40


def test_snapshot_id_is_derived_from_project_and_fetch_time():
    snap = _snapshot()
    assert snap.snapshot_id == "oakley-20260824T000000Z"


def test_frame_with_mismatched_column_lengths_raises():
    results = _results()
    results["by_pagetype"]["frames"][0]["data"]["values"][1] = [1200]
    with pytest.raises(ParseError):
        _snapshot(results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/grafana_parse_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest.grafana.parse'`.

- [ ] **Step 3: Write minimal implementation**

Create `ingest/grafana/parse.py`:

```python
"""Grafana's column-major frames to :class:`FieldSnapshot`.

The wire shape is::

    results.<refId>.frames[].schema.fields[]  -> names and types
    results.<refId>.frames[].data.values[]    -> parallel arrays, one per field

Columns are matched by **name**, never by position, so a column added to a
panel does not silently shift every value one to the left. A field the schema
does not carry yields ``None`` rather than a zero.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from normalize.field import (
    AssetRow,
    CountryRow,
    DeviceRow,
    FieldSnapshot,
    FieldVitals,
    Frustration,
    InpBucketRow,
    PageTypeRow,
    SessionKpis,
    TimePoint,
)


class ParseError(Exception):
    """A response this module cannot turn into a snapshot."""


def frame_rows(result: Any) -> List[Dict[str, Any]]:
    """Every frame of one panel, transposed to row dicts keyed by field name."""
    if not isinstance(result, dict):
        return []
    rows: List[Dict[str, Any]] = []
    for frame in result.get("frames") or []:
        fields = ((frame.get("schema") or {}).get("fields")) or []
        columns = (frame.get("data") or {}).get("values") or []
        if not fields or not columns:
            continue
        if len(fields) != len(columns):
            raise ParseError(
                f"Frame declares {len(fields)} fields but carries "
                f"{len(columns)} columns."
            )
        lengths = {len(column) for column in columns}
        if len(lengths) > 1:
            raise ParseError(f"Frame columns have differing lengths: {sorted(lengths)}")
        names = [str(field.get("name", "")) for field in fields]
        for index in range(lengths.pop() if lengths else 0):
            rows.append({name: columns[i][index] for i, name in enumerate(names)})
    return rows


def _num(row: Dict[str, Any], key: str) -> Optional[float]:
    """A numeric cell, or None. Never coerces a missing value to zero."""
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(row: Dict[str, Any], key: str) -> Optional[int]:
    value = _num(row, key)
    return None if value is None else int(round(value))


def _points(rows: Sequence[Dict[str, Any]]) -> List[TimePoint]:
    """Rows carrying a ``time`` column become the series, oldest first."""
    points: List[TimePoint] = []
    for row in rows:
        raw = row.get("time")
        if raw is None:
            continue
        # Grafana sends epoch milliseconds for time-typed fields.
        moment = datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
        points.append(TimePoint(
            time=moment,
            values={k: _num(row, k) for k in row if k != "time"},
        ))
    return sorted(points, key=lambda p: p.time)


def _last(points: Sequence[TimePoint], key: str) -> Optional[float]:
    """The newest non-null value for one column — the dashboard's reducer."""
    for point in reversed(points):
        value = point.values.get(key)
        if value is not None:
            return value
    return None


def build_snapshot(
    results: Dict[str, Any],
    *,
    project: str,
    hosts: Sequence[str],
    window_from: datetime,
    window_to: datetime,
    fetched_at: datetime,
) -> FieldSnapshot:
    """Assemble one snapshot from a full ``/api/ds/query`` results map."""
    session_points = _points(frame_rows(results.get("sessions")))
    vital_points = _points(frame_rows(results.get("vitals")))
    frustration_points = _points(frame_rows(results.get("frustration")))

    sessions = SessionKpis(
        bounce_pct=_last(session_points, "bounce_pct"),
        conversion_pct=_last(session_points, "conversion_pct"),
        avg_session_pages=_last(session_points, "avg_session_pages"),
        session_count=(
            int(sum(p.values.get("session_count") or 0 for p in session_points))
            if session_points else None
        ),
        series=session_points,
    )

    vitals = FieldVitals(
        **{
            key: _last(vital_points, key)
            for key in ("lcp_p75", "lcp_p95", "inp_p75", "inp_p95",
                        "cls_p75", "cls_p95", "ttfb_p75", "plt_p75")
        },
        series=vital_points,
    )

    frustration = Frustration(
        rage_clicks_total=(
            int(sum(p.values.get("rage_clicks") or 0 for p in frustration_points))
            if frustration_points else None
        ),
        frustration_p75=_last(frustration_points, "frustration_p75"),
        series=frustration_points,
    )

    def segment(ref_id, model, key_field, key_name, extra=()):
        built = []
        for row in frame_rows(results.get(ref_id)):
            label = row.get(key_field)
            if not label:
                continue
            fields = {
                key_name: str(label),
                "beacons": _int(row, "beacons"),
                "lcp_p75": _num(row, "lcp_p75"),
                "inp_p75": _num(row, "inp_p75"),
                "cls_p75": _num(row, "cls_p75"),
                "plt_p75": _num(row, "plt_p75"),
                "ttfb_p75": _num(row, "ttfb_p75"),
                "frustration_p75": _num(row, "frustration_p75"),
                "rage_clicks": _int(row, "rage_clicks"),
            }
            for name, reader in extra:
                fields[name] = reader(row)
            built.append(model(**fields))
        return built

    return FieldSnapshot(
        snapshot_id=f"{project}-{fetched_at.strftime('%Y%m%dT%H%M%SZ')}",
        project=project,
        hosts=list(hosts),
        window_from=window_from,
        window_to=window_to,
        fetched_at=fetched_at,
        sessions=sessions,
        vitals=vitals,
        frustration=frustration,
        by_device=segment(
            "by_device", DeviceRow, "device", "device",
            extra=(("rage_pct", lambda r: _num(r, "rage_pct")),
                   ("avg_session_pages", lambda r: _num(r, "avg_session_pages"))),
        ),
        by_country=segment("by_country", CountryRow, "country", "country"),
        by_pagetype=segment(
            "by_pagetype", PageTypeRow, "page_group", "page_group",
            extra=(("bounce_pct", lambda r: _num(r, "bounce_pct")),),
        ),
        inp_buckets=[
            InpBucketRow(
                bucket=str(row.get("bucket")),
                beacons=_int(row, "beacons"),
                avg_frustration=_num(row, "avg_frustration"),
                rage_session_pct=_num(row, "rage_session_pct"),
            )
            for row in frame_rows(results.get("inp_buckets"))
            if row.get("bucket")
        ],
        assets=[
            AssetRow(
                asset_type=str(row.get("asset_type")),
                request_count=_int(row, "request_count"),
                avg_size_kb=_num(row, "avg_size_kb"),
                edge_ms=_num(row, "edge_ms"),
                origin_ms=_num(row, "origin_ms"),
                cache_hit_pct=_num(row, "cache_hit_pct"),
            )
            for row in frame_rows(results.get("assets"))
            if row.get("asset_type")
        ],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/grafana_parse_test.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add ingest/grafana/parse.py tests/unit/grafana_parse_test.py tests/fixtures/
git commit -m "Parse Grafana frames into FieldSnapshot by column name"
```

---

### Task 6: Persistence

**Files:**
- Modify: `store/sql.py` (extend `SCHEMA`, add three functions)
- Test: `tests/integration/field_store_test.py`

**Interfaces:**
- Consumes: `normalize.field.FieldSnapshot`.
- Produces: `store.sql.insert_snapshot(conn, snapshot, *, replace=False) -> str`, `get_latest_snapshot(conn, project) -> Optional[FieldSnapshot]`, `list_snapshots(conn, *, project=None) -> List[FieldSnapshot]`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/field_store_test.py`:

```python
"""field_snapshots round-trip through SQLite."""
from datetime import datetime, timedelta, timezone

import pytest

from normalize.field import FieldSnapshot, PageTypeRow, SessionKpis
from store import sql

BASE = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


def _snap(snapshot_id="oakley-1", project="oakley", fetched_at=BASE, **kw):
    return FieldSnapshot(
        snapshot_id=snapshot_id,
        project=project,
        hosts=["www.oakley.com"],
        window_from=fetched_at - timedelta(days=7),
        window_to=fetched_at,
        fetched_at=fetched_at,
        **kw,
    )


@pytest.fixture
def conn():
    connection = sql.connect(":memory:")
    sql.init_schema(connection)
    yield connection
    connection.close()


def test_snapshot_round_trips_with_nested_rows(conn):
    original = _snap(
        sessions=SessionKpis(bounce_pct=44.5, session_count=9100),
        by_pagetype=[PageTypeRow(page_group="Pdp", beacons=1200, lcp_p75=4100.0)],
    )
    sql.insert_snapshot(conn, original)

    loaded = sql.get_latest_snapshot(conn, "oakley")
    assert loaded.sessions.bounce_pct == 44.5
    assert loaded.page_row("Pdp").lcp_p75 == 4100.0
    assert loaded.window_from == original.window_from


def test_unmeasured_stays_none_across_the_round_trip(conn):
    sql.insert_snapshot(conn, _snap())
    loaded = sql.get_latest_snapshot(conn, "oakley")
    assert loaded.sessions.bounce_pct is None
    assert loaded.vitals.lcp_p75 is None


def test_latest_is_by_fetched_at_not_insertion_order(conn):
    sql.insert_snapshot(conn, _snap("old", fetched_at=BASE - timedelta(days=2)))
    sql.insert_snapshot(conn, _snap("new", fetched_at=BASE))
    sql.insert_snapshot(conn, _snap("mid", fetched_at=BASE - timedelta(days=1)))
    assert sql.get_latest_snapshot(conn, "oakley").snapshot_id == "new"


def test_latest_is_scoped_to_project(conn):
    sql.insert_snapshot(conn, _snap("o", project="oakley"))
    sql.insert_snapshot(conn, _snap("r", project="rayban",
                                    fetched_at=BASE + timedelta(days=1)))
    assert sql.get_latest_snapshot(conn, "oakley").snapshot_id == "o"


def test_missing_project_returns_none(conn):
    assert sql.get_latest_snapshot(conn, "nobody") is None


def test_duplicate_id_raises_unless_replacing(conn):
    sql.insert_snapshot(conn, _snap())
    with pytest.raises(sql.StoreError):
        sql.insert_snapshot(conn, _snap())
    sql.insert_snapshot(conn, _snap(sessions=SessionKpis(bounce_pct=1.0)),
                        replace=True)
    assert sql.get_latest_snapshot(conn, "oakley").sessions.bounce_pct == 1.0


def test_list_snapshots_is_newest_first(conn):
    sql.insert_snapshot(conn, _snap("a", fetched_at=BASE - timedelta(days=1)))
    sql.insert_snapshot(conn, _snap("b", fetched_at=BASE))
    assert [s.snapshot_id for s in sql.list_snapshots(conn)] == ["b", "a"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/field_store_test.py -v`
Expected: FAIL — `AttributeError: module 'store.sql' has no attribute 'insert_snapshot'`.

- [ ] **Step 3: Write minimal implementation**

In `store/sql.py`, append to the `SCHEMA` string (before the closing `"""`):

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

Add at the end of the module:

```python
# --------------------------------------------------------------------------- #
# Field snapshots
# --------------------------------------------------------------------------- #
def insert_snapshot(
    conn: sqlite3.Connection, snapshot: "FieldSnapshot", *, replace: bool = False
) -> str:
    """Persist one field snapshot.

    Same split the ``runs`` table uses: columns for querying, one JSON payload
    for fidelity. The snapshot is a document — it is read whole or not at all —
    so decomposing eight row lists into eight tables would buy nothing and cost
    a migration every time a panel gains a column.
    """
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    try:
        conn.execute(
            f"{verb} INTO field_snapshots "
            "(snapshot_id, project, fetched_at, window_from, window_to, "
            " hosts, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.snapshot_id,
                snapshot.project,
                snapshot.fetched_at.isoformat(),
                snapshot.window_from.isoformat(),
                snapshot.window_to.isoformat(),
                json.dumps(snapshot.hosts),
                json.dumps(snapshot.model_dump(mode="json")),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise StoreError(
            f"Field snapshot {snapshot.snapshot_id} already exists. "
            "Pass replace=True to overwrite it."
        ) from exc
    return snapshot.snapshot_id


def _snapshot_from_row(row: Any) -> "FieldSnapshot":
    from normalize.field import FieldSnapshot

    return FieldSnapshot.model_validate(json.loads(row["payload"]))


def get_latest_snapshot(
    conn: sqlite3.Connection, project: str
) -> Optional["FieldSnapshot"]:
    """The most recently *fetched* snapshot for a project, or None.

    Ordered by ``fetched_at`` rather than insertion order: backfilling an older
    window must not make it look like the current one.
    """
    row = conn.execute(
        "SELECT payload FROM field_snapshots WHERE project = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (project,),
    ).fetchone()
    return None if row is None else _snapshot_from_row(row)


def list_snapshots(
    conn: sqlite3.Connection, *, project: Optional[str] = None
) -> List["FieldSnapshot"]:
    """Every stored snapshot, newest first."""
    if project is None:
        rows = conn.execute(
            "SELECT payload FROM field_snapshots ORDER BY fetched_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT payload FROM field_snapshots WHERE project = ? "
            "ORDER BY fetched_at DESC",
            (project,),
        ).fetchall()
    return [_snapshot_from_row(row) for row in rows]
```

Add `from normalize.field import FieldSnapshot` under `TYPE_CHECKING` at the top if the module uses that guard; otherwise the local imports above suffice. Confirm `conn.row_factory` is set to `sqlite3.Row` in `connect()` — `_snapshot_from_row` indexes by name.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/field_store_test.py tests/integration/store_test.py -v`
Expected: PASS — both the new tests and the pre-existing store tests, proving the schema addition did not disturb `runs`.

- [ ] **Step 5: Commit**

```bash
git add store/sql.py tests/integration/field_store_test.py
git commit -m "Persist field snapshots beside the runs"
```

---

### Task 7: The `ingest field` stage and CLI wiring

**Files:**
- Create: `ingest/field.py`
- Modify: `cli.py` (add to `COMMANDS`, `_INGEST_MODES`, `_DELEGATES`)
- Test: `tests/integration/field_pipeline_test.py`, `tests/unit/cli_test.py`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: `ingest.field.fetch_snapshot(settings, *, project, hosts, env=None, client=None, now=None) -> FieldSnapshot`, `ingest.field.hosts_for(targets) -> List[str]`, `ingest.field.main(argv) -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/field_pipeline_test.py`:

```python
"""ingest field: hosts from targets.yaml, fetch, persist. No network."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from config.load import Settings
from ingest.field import fetch_snapshot, hosts_for, main
from store import sql

FIXTURE = Path(__file__).parent.parent / "fixtures" / "grafana_response.json"


class _StubClient:
    """Replays the recorded response for whatever SQL it is handed."""

    def __init__(self):
        self.calls = []

    def query(self, sql_by_ref, *, window):
        self.calls.append((sorted(sql_by_ref), window))
        return json.loads(FIXTURE.read_text(encoding="utf-8"))["results"]


class _Targets:
    def __init__(self, pages):
        self.pages = pages


class _Page:
    def __init__(self, url):
        self.url = url


def test_hosts_are_derived_from_target_urls_deduped_and_sorted():
    targets = _Targets([
        _Page("https://www.oakley.com/en-us"),
        _Page("https://www.oakley.com/en-us/category/sunglasses"),
        _Page("https://m.oakley.com/x"),
    ])
    assert hosts_for(targets) == ["m.oakley.com", "www.oakley.com"]


def test_hosts_ignores_a_page_with_no_hostname():
    assert hosts_for(_Targets([_Page("not-a-url"), _Page("https://a.com/x")])) == ["a.com"]


def test_fetch_issues_one_call_carrying_all_eight_queries():
    client = _StubClient()
    snapshot = fetch_snapshot(
        Settings(), project="oakley", hosts=["www.oakley.com"], client=client,
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    refs, window = client.calls[0]
    assert len(client.calls) == 1
    assert len(refs) == 8
    assert window == "7d"
    assert snapshot.project == "oakley"


def test_window_is_resolved_to_absolute_utc():
    """'now-7d' is not reproducible; the stored snapshot must name its week."""
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    snapshot = fetch_snapshot(
        Settings(), project="oakley", hosts=["www.oakley.com"],
        client=_StubClient(), now=now,
    )
    assert snapshot.window_to == now
    assert (snapshot.window_to - snapshot.window_from).days == 7


def test_main_persists_and_reports(tmp_path, monkeypatch, capsys):
    store_path = tmp_path / "runs.sqlite"
    monkeypatch.setattr("ingest.field._build_client", lambda settings: _StubClient())
    monkeypatch.setattr(
        "ingest.field.load_settings",
        lambda *a, **k: Settings(storage={"sqlite_path": str(store_path)}),
    )
    assert main(["--project", "oakley", "--hosts", "www.oakley.com"]) == 0

    conn = sql.connect(store_path)
    sql.init_schema(conn)
    try:
        assert sql.get_latest_snapshot(conn, "oakley") is not None
    finally:
        conn.close()
    assert "www.oakley.com" in capsys.readouterr().out


def test_main_exits_non_zero_when_grafana_is_unreachable(tmp_path, monkeypatch):
    from ingest.grafana.client import GrafanaError

    def _boom(settings):
        raise GrafanaError("could not reach Grafana")

    monkeypatch.setattr("ingest.field._build_client", _boom)
    monkeypatch.setattr(
        "ingest.field.load_settings",
        lambda *a, **k: Settings(storage={"sqlite_path": str(tmp_path / "r.sqlite")}),
    )
    assert main(["--project", "oakley", "--hosts", "www.oakley.com"]) == 1
```

Append to `tests/unit/cli_test.py`:

```python
def test_ingest_field_is_a_known_mode():
    import cli

    assert "ingest field" in cli.COMMANDS
    assert "ingest field" in cli._DELEGATES
    assert "field" in cli._INGEST_MODES


def test_ingest_field_forwards_argv_verbatim(monkeypatch):
    import cli

    seen = {}
    monkeypatch.setitem(
        cli._DELEGATES, "ingest field",
        lambda: (lambda argv: seen.setdefault("argv", argv) or 0),
    )
    assert cli.main(["ingest", "field", "--project", "oakley"]) == 0
    assert seen["argv"] == ["--project", "oakley"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/field_pipeline_test.py tests/unit/cli_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest.field'`.

- [ ] **Step 3: Write minimal implementation**

Create `ingest/field.py`:

```python
"""``python -m cli ingest field`` — Grafana in, FieldSnapshot in the store.

A third ingestion door beside ``ingest auto`` and ``ingest manual``. Unlike
those two it produces no :class:`Run`: field data is session-scoped and
brand-wide, so it lands in ``field_snapshots`` instead.

This stage **does** exit non-zero on failure, unlike analysis. A bad token, an
unreachable host or a rejected query are all things a user can fix, and the
existing convention is that those are errors.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence
from urllib.parse import urlsplit

from config.load import load_settings, load_targets
from ingest.grafana import parse, queries
from ingest.grafana.client import GrafanaClient, GrafanaError, resolve_grafana_env
from normalize.field import FieldSnapshot

#: Grafana relative-window suffixes, in seconds.
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def window_delta(window: str) -> timedelta:
    """``7d`` -> 7 days. Raises on anything this stage cannot resolve."""
    text = window.strip().lower()
    if len(text) < 2 or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise ValueError(
            f"Unsupported window {window!r}. Use a number followed by "
            "m, h, d or w — for example 7d."
        )
    return timedelta(seconds=int(text[:-1]) * _UNITS[text[-1]])


def hosts_for(targets: Any) -> List[str]:
    """The hostnames of every configured page, deduplicated and sorted.

    Derived rather than configured separately: a second list of hosts drifts,
    and when it does the report compares lab measurements of one site against
    field measurements of another while reading perfectly plausibly.
    """
    found = set()
    for page in getattr(targets, "pages", []):
        host = urlsplit(getattr(page, "url", "")).hostname
        if host:
            found.add(host.lower())
    return sorted(found)


def _build_client(settings: Any) -> GrafanaClient:
    """The real client. Separated so tests substitute it without patching env."""
    return GrafanaClient(
        resolve_grafana_env(), timeout_s=settings.grafana.timeout_s
    )


def fetch_snapshot(
    settings: Any,
    *,
    project: str,
    hosts: Sequence[str],
    client: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> FieldSnapshot:
    """Render every query, issue one request, parse the result."""
    client = client or _build_client(settings)
    window = settings.grafana.window
    fetched_at = now or datetime.now(timezone.utc)

    env_table = getattr(getattr(client, "_env", None), "table", "") or "{table}"
    sql_by_ref = queries.render_all(table=env_table, hosts=hosts)
    results = client.query(sql_by_ref, window=window)

    return parse.build_snapshot(
        results,
        project=project,
        hosts=hosts,
        window_from=fetched_at - window_delta(window),
        window_to=fetched_at,
        fetched_at=fetched_at,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m cli ingest field",
        description="Fetch real-user (RUM) data from Grafana and store it.",
    )
    p.add_argument("--project", default=None,
                   help="Project name (default: from config/targets.yaml).")
    p.add_argument("--hosts", default=None,
                   help="Comma-separated hosts (default: derived from targets).")
    p.add_argument("--settings", default=None, help="Path to settings.yaml.")
    p.add_argument("--targets", default=None, help="Path to targets.yaml.")
    p.add_argument("--replace", action="store_true",
                   help="Overwrite a snapshot with the same id.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = load_settings(args.settings) if args.settings else load_settings()

    project = args.project
    hosts: List[str]
    if args.hosts:
        hosts = sorted({h.strip().lower() for h in args.hosts.split(",") if h.strip()})
    else:
        targets = load_targets(args.targets) if args.targets else load_targets()
        hosts = hosts_for(targets)
        project = project or targets.project

    if not project:
        print("No project name. Pass --project or configure targets.yaml.",
              file=sys.stderr)
        return 2
    if not hosts:
        print("No hosts to query. Pass --hosts or configure pages in "
              "targets.yaml.", file=sys.stderr)
        return 2

    try:
        snapshot = fetch_snapshot(settings, project=project, hosts=hosts)
    except (GrafanaError, ValueError, parse.ParseError, queries.HostError) as exc:
        print(f"Field ingestion failed: {exc}", file=sys.stderr)
        return 1

    from store import sql

    conn = sql.connect(settings.storage.sqlite_path)
    sql.init_schema(conn)
    try:
        sql.insert_snapshot(conn, snapshot, replace=args.replace)
    finally:
        conn.close()

    print(
        f"Stored field snapshot {snapshot.snapshot_id} for "
        f"{', '.join(snapshot.hosts)} covering "
        f"{snapshot.window_from:%Y-%m-%d} to {snapshot.window_to:%Y-%m-%d} "
        f"({len(snapshot.by_pagetype)} page groups, "
        f"{len(snapshot.by_country)} countries)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
```

In `cli.py`, add to `COMMANDS` after `"ingest manual"`:

```python
    "ingest field": "Fetch real-user (RUM) data from Grafana into the store",
```

extend the modes tuple:

```python
_INGEST_MODES = ("auto", "manual", "field")
```

add the loader beside `_ingest_manual`:

```python
def _ingest_field() -> Delegate:
    from ingest.field import main
    return main
```

and register it in `_DELEGATES`:

```python
    "ingest field": _ingest_field,
```

> **Check before implementing:** confirm `config.load` exports `load_targets` and that `TargetsConfig` has `.project` and `.pages`. Task 1's reading of `config/load.py` shows both; if the loader is named differently, use the actual name and keep `hosts_for`'s duck-typed signature so the test's `_Targets` stub still applies.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/field_pipeline_test.py tests/unit/cli_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ingest/field.py cli.py tests/integration/field_pipeline_test.py tests/unit/cli_test.py
git commit -m "Add the ingest field stage and wire it into the CLI"
```

---

### Task 8: Field symptom rules

**Files:**
- Modify: `rag/retrieve.py` (add `detect_field_symptoms`)
- Test: `tests/unit/field_symptoms_test.py`

**Interfaces:**
- Consumes: `normalize.field.FieldSnapshot`, `config.load.Thresholds`.
- Produces: `rag.retrieve.detect_field_symptoms(snapshot, *, page_group=None, thresholds=None) -> List[Symptom]`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/field_symptoms_test.py`:

```python
"""Field rules are rule-based, so the no-LLM path keeps the feature."""
from datetime import datetime, timezone

from config.load import Thresholds
from normalize.field import (
    AssetRow, FieldSnapshot, InpBucketRow, PageTypeRow, SessionKpis,
)
from rag.retrieve import detect_field_symptoms

NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _snap(**kw):
    return FieldSnapshot(
        snapshot_id="s1", project="oakley", hosts=["www.oakley.com"],
        window_from=NOW, window_to=NOW, fetched_at=NOW, **kw,
    )


def test_no_data_yields_no_symptoms():
    assert detect_field_symptoms(_snap()) == []


def test_page_group_bounce_above_site_wide_by_the_margin_fires():
    snap = _snap(
        sessions=SessionKpis(bounce_pct=40.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=61.5)],
    )
    codes = [s.code for s in detect_field_symptoms(snap, page_group="Pdp")]
    assert "field_bounce_high" in codes


def test_bounce_within_the_margin_does_not_fire():
    snap = _snap(
        sessions=SessionKpis(bounce_pct=40.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=45.0)],
    )
    assert detect_field_symptoms(snap, page_group="Pdp") == []


def test_margin_is_percentage_points_not_a_ratio():
    """2% -> 4% doubles but is only 2pp: a low-traffic group must not scream."""
    snap = _snap(
        sessions=SessionKpis(bounce_pct=2.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=4.0)],
    )
    assert detect_field_symptoms(snap, page_group="Pdp") == []


def test_low_cache_hit_ratio_names_the_asset_type():
    snap = _snap(assets=[
        AssetRow(asset_type="Images", cache_hit_pct=48.0, origin_ms=900.0),
        AssetRow(asset_type="CSS", cache_hit_pct=96.0),
    ])
    symptoms = detect_field_symptoms(snap)
    assert any(s.code == "field_cache_low" and "Images" in s.text
               for s in symptoms)
    assert not any("CSS" in s.text for s in symptoms)


def test_frustration_severity_splits_at_the_configured_thresholds():
    warn = detect_field_symptoms(
        _snap(by_pagetype=[PageTypeRow(page_group="Pdp", frustration_p75=40.0)]),
        page_group="Pdp",
    )
    fail = detect_field_symptoms(
        _snap(by_pagetype=[PageTypeRow(page_group="Pdp", frustration_p75=70.0)]),
        page_group="Pdp",
    )
    assert [s.severity for s in warn if s.code.startswith("field_frustration")] == ["warn"]
    assert [s.severity for s in fail if s.code.startswith("field_frustration")] == ["fail"]


def test_worst_inp_bucket_with_climbing_frustration_fires():
    snap = _snap(inp_buckets=[
        InpBucketRow(bucket="1 - under 200 ms (good)", beacons=900, avg_frustration=12.0),
        InpBucketRow(bucket="4 - over 1000 ms (critical)", beacons=300, avg_frustration=71.0),
    ])
    codes = [s.code for s in detect_field_symptoms(snap)]
    assert "field_inp_frustration" in codes


def test_symptoms_are_ordered_fail_before_warn_and_deterministic():
    snap = _snap(
        sessions=SessionKpis(bounce_pct=30.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=70.0,
                                 frustration_p75=70.0)],
        assets=[AssetRow(asset_type="Images", cache_hit_pct=10.0)],
    )
    first = detect_field_symptoms(snap, page_group="Pdp")
    assert [s.severity for s in first] == sorted(
        [s.severity for s in first], key=lambda sev: 0 if sev == "fail" else 1
    )
    assert [s.code for s in first] == [
        s.code for s in detect_field_symptoms(snap, page_group="Pdp")
    ]


def test_custom_thresholds_are_honoured():
    snap = _snap(
        sessions=SessionKpis(bounce_pct=40.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=45.0)],
    )
    th = Thresholds(field_bounce_excess_pp=2.0)
    codes = [s.code for s in detect_field_symptoms(snap, page_group="Pdp",
                                                   thresholds=th)]
    assert "field_bounce_high" in codes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/field_symptoms_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'detect_field_symptoms'`.

- [ ] **Step 3: Write minimal implementation**

Append to `rag/retrieve.py`:

```python
def detect_field_symptoms(
    snapshot: "FieldSnapshot",
    *,
    page_group: Optional[str] = None,
    thresholds: Optional[Thresholds] = None,
) -> List[Symptom]:
    """Threshold-backed statements about *real users*, not the lab.

    Rule-based for the same reason :func:`detect_symptoms` is: a campaign
    degraded to the no-LLM path by an exhausted budget still surfaces field
    findings instead of losing the feature entirely.

    ``page_group`` scopes the per-page rules to one mPulse page group; the
    asset and INP-bucket rules are brand-wide and always evaluated.
    """
    th = thresholds or Thresholds()
    found: List[Symptom] = []

    def add(code, text, severity, metric=None, value=None, target=None):
        found.append(Symptom(code, text, severity, metric, value, target))

    row = snapshot.page_row(page_group) if page_group else None
    site_bounce = snapshot.sessions.bounce_pct

    if row is not None and row.bounce_pct is not None and site_bounce is not None:
        excess = row.bounce_pct - site_bounce
        if excess >= th.field_bounce_excess_pp:
            add("field_bounce_high",
                f"{_fmt(row.bounce_pct)}% of real visitors who land on this page "
                f"leave without going any further, against {_fmt(site_bounce)}% "
                "across the site - they are giving up here specifically.",
                "fail" if excess >= th.field_bounce_excess_pp * 2 else "warn",
                "bounce_pct", row.bounce_pct, site_bounce)

    if row is not None and row.frustration_p75 is not None:
        if row.frustration_p75 >= th.field_frustration_fail:
            add("field_frustration_fail",
                f"Real users register a frustration index of "
                f"{_fmt(row.frustration_p75)} on this page - repeated clicks on "
                "things that did not respond.",
                "fail", "frustration_p75", row.frustration_p75,
                th.field_frustration_warn)
        elif row.frustration_p75 >= th.field_frustration_warn:
            add("field_frustration_warn",
                f"Real users register a frustration index of "
                f"{_fmt(row.frustration_p75)} on this page.",
                "warn", "frustration_p75", row.frustration_p75,
                th.field_frustration_warn)

    for asset in snapshot.assets:
        if asset.cache_hit_pct is None:
            continue
        if asset.cache_hit_pct < th.field_cache_hit_warn_pct:
            origin = (f" and each miss costs {_fmt(asset.origin_ms)}ms at the origin"
                      if asset.origin_ms else "")
            add("field_cache_low",
                f"Only {_fmt(asset.cache_hit_pct)}% of {asset.asset_type} requests "
                f"are served from the CDN edge{origin} - real users are waiting "
                "for content that could have been cached.",
                "fail" if asset.cache_hit_pct < th.field_cache_hit_warn_pct / 2
                else "warn",
                "cache_hit_pct", asset.cache_hit_pct, th.field_cache_hit_warn_pct)

    worst = max(
        (b for b in snapshot.inp_buckets if b.avg_frustration is not None),
        key=lambda b: b.avg_frustration, default=None,
    )
    if worst is not None and worst.avg_frustration >= th.field_frustration_fail:
        add("field_inp_frustration",
            f"Visitors whose interactions fall in the '{worst.bucket}' band show "
            f"an average frustration of {_fmt(worst.avg_frustration)} - slow "
            "responses are translating into real irritation.",
            "fail", "frustration_p75", worst.avg_frustration,
            th.field_frustration_fail)

    # Same ordering contract as detect_symptoms: severity, then code, so the
    # query text and therefore retrieval are deterministic for equal input.
    return sorted(found, key=lambda s: (0 if s.severity == "fail" else 1, s.code))
```

Add the import guard at the top of `rag/retrieve.py`:

```python
if TYPE_CHECKING:  # pragma: no cover
    from normalize.field import FieldSnapshot
```

adding `TYPE_CHECKING` to the existing `typing` import.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/field_symptoms_test.py tests/unit/rag_test.py -v`
Expected: PASS — new rules plus the untouched existing retrieval tests.

- [ ] **Step 5: Commit**

```bash
git add rag/retrieve.py tests/unit/field_symptoms_test.py
git commit -m "Detect field symptoms from thresholds, not from a model"
```

---

### Task 9: Field data in the grounded prompt

**Files:**
- Modify: `rag/prompt.py` (add `format_field_measurements`, extend `build_analysis_prompt`)
- Test: `tests/unit/rag_test.py`

**Interfaces:**
- Consumes: `normalize.field.FieldSnapshot`.
- Produces: `rag.prompt.format_field_measurements(snapshot, *, page_group=None, max_rows=6) -> str`; `build_analysis_prompt(..., field: Optional[FieldSnapshot] = None, page_group: Optional[str] = None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/rag_test.py`:

```python
def _field_snapshot():
    from datetime import datetime, timezone

    from normalize.field import (
        AssetRow, CountryRow, FieldSnapshot, PageTypeRow, SessionKpis, FieldVitals,
    )

    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    return FieldSnapshot(
        snapshot_id="s1", project="oakley", hosts=["www.oakley.com"],
        window_from=now, window_to=now, fetched_at=now,
        sessions=SessionKpis(bounce_pct=41.0, conversion_pct=2.4,
                             avg_session_pages=3.1, session_count=91000),
        vitals=FieldVitals(lcp_p75=3900.0, inp_p75=240.0, cls_p75=0.12),
        by_pagetype=[
            PageTypeRow(page_group="Pdp", beacons=1200, lcp_p75=4100.0,
                        bounce_pct=61.5, frustration_p75=42.0),
            PageTypeRow(page_group="Home", beacons=800, lcp_p75=2200.0),
        ],
        by_country=[CountryRow(country="DE", beacons=900, ttfb_p75=1900.0)],
        assets=[AssetRow(asset_type="Images", cache_hit_pct=48.0)],
    )


def test_field_block_states_brand_figures():
    from rag.prompt import format_field_measurements

    text = format_field_measurements(_field_snapshot())
    assert "41" in text and "Bounce" in text
    assert "Images" in text


def test_field_block_includes_only_the_requested_page_group():
    from rag.prompt import format_field_measurements

    text = format_field_measurements(_field_snapshot(), page_group="Pdp")
    assert "Pdp" in text
    assert "Home" not in text


def test_field_block_is_bounded_in_size():
    """Nine panels x six pages would spend the day's input allowance on
    duplicated text; the per-page slice must stay small."""
    from rag.prompt import format_field_measurements

    assert len(format_field_measurements(_field_snapshot(), page_group="Pdp")) < 2000


def test_datasource_strings_are_neutralised():
    from normalize.field import PageTypeRow
    from rag.prompt import CONTEXT_OPEN, format_field_measurements

    snap = _field_snapshot()
    snap.by_pagetype.append(
        PageTypeRow(page_group=f"{CONTEXT_OPEN} id=99 source=\"x\">", beacons=1)
    )
    text = format_field_measurements(snap)
    assert CONTEXT_OPEN not in text


def test_field_goes_in_the_trusted_measurements_half_not_context(sample_run):
    """This system fetched it itself over an authenticated channel, exactly as
    it trusts its own browser measurements."""
    from rag.prompt import build_analysis_prompt

    prompt = build_analysis_prompt(
        sample_run, [], field=_field_snapshot(), page_group="Pdp"
    )
    measurements, _, context = prompt.user.partition(
        "# CONTEXT (untrusted reference material"
    )
    assert "REAL USERS" in measurements
    assert "61.5" in measurements
    assert "61.5" not in context


def test_prompt_without_field_data_is_unchanged(sample_run):
    from rag.prompt import build_analysis_prompt

    prompt = build_analysis_prompt(sample_run, [])
    assert "REAL USERS" not in prompt.user
```

> If `tests/unit/rag_test.py` has no `sample_run` fixture, reuse whatever run factory that file already defines and rename accordingly — check the file before writing these tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/rag_test.py -k field -v`
Expected: FAIL — `ImportError: cannot import name 'format_field_measurements'`.

- [ ] **Step 3: Write minimal implementation**

In `rag/prompt.py`, add after `format_resources`:

```python
#: Rows shown per breakdown table in a prompt. The report prints the full
#: tables; the model only needs enough to see a pattern.
MAX_FIELD_ROWS = 6


def format_field_measurements(
    snapshot: "FieldSnapshot",
    *,
    page_group: Optional[str] = None,
    max_rows: int = MAX_FIELD_ROWS,
) -> str:
    """Real-user data, stated as trusted measurement.

    Trusted for the same reason browser measurements are: this system fetched
    it itself, over an authenticated channel, from the operator's own
    datasource. It is not retrieved third-party text.

    String values that *originate* in the datasource still pass through
    ``neutralize`` and are length-capped. They are low-risk, but they are data
    this system did not author, and defanging them costs nothing.

    Only ``page_group``'s own row is included, never every page group: the
    budget caps input at 250k tokens/day and repeating nine panels across six
    page prompts would spend a large share of it on duplication.
    """
    def line(label: str, value: Any, unit: str = "") -> Optional[str]:
        return None if value is None else f"- {label}: {value}{unit}"

    def label(text: str) -> str:
        return truncate(neutralize(str(text)), 60)

    sessions, vitals = snapshot.sessions, snapshot.vitals
    rows: List[Optional[str]] = [
        f"Window: {snapshot.window_from:%Y-%m-%d} to {snapshot.window_to:%Y-%m-%d}, "
        f"hosts {', '.join(snapshot.hosts)}",
        "",
        "Site-wide, real users:",
        line("Sessions", sessions.session_count),
        line("Bounce rate", sessions.bounce_pct, "%"),
        line("Conversion rate", sessions.conversion_pct, "%"),
        line("Pages per session", sessions.avg_session_pages),
        line("LCP p75", vitals.lcp_p75, "ms"),
        line("INP p75", vitals.inp_p75, "ms"),
        line("CLS p75", vitals.cls_p75),
        line("TTFB p75", vitals.ttfb_p75, "ms"),
    ]

    row = snapshot.page_row(page_group) if page_group else None
    if row is not None:
        rows += [
            "",
            f"This page ({label(row.page_group)}), real users:",
            line("Beacons", row.beacons),
            line("LCP p75", row.lcp_p75, "ms"),
            line("INP p75", row.inp_p75, "ms"),
            line("CLS p75", row.cls_p75),
            line("Entry bounce rate", row.bounce_pct, "%"),
            line("Frustration index p75", row.frustration_p75),
            line("Rage clicks", row.rage_clicks),
        ]

    slow_assets = [
        a for a in snapshot.assets
        if a.cache_hit_pct is not None or a.origin_ms is not None
    ][:max_rows]
    if slow_assets:
        rows += ["", "CDN behaviour by asset type:"]
        rows += [
            f"- {label(a.asset_type)}: cache hit "
            f"{'unknown' if a.cache_hit_pct is None else f'{a.cache_hit_pct}%'}, "
            f"origin {'unknown' if a.origin_ms is None else f'{a.origin_ms}ms'}"
            for a in slow_assets
        ]

    worst_countries = [c for c in snapshot.by_country if c.ttfb_p75][:max_rows]
    if worst_countries:
        rows += ["", "Slowest markets by TTFB p75:"]
        rows += [
            f"- {label(c.country)}: {c.ttfb_p75}ms over {c.beacons} beacons"
            for c in worst_countries
        ]

    return "\n".join(r for r in rows if r is not None)
```

Extend `build_analysis_prompt`'s signature with `field: Optional["FieldSnapshot"] = None, page_group: Optional[str] = None`, and insert immediately after the `resources` block:

```python
    if field is not None:
        sections += [
            "",
            "# REAL USERS (trusted, fetched by this system from Grafana/mPulse)",
            format_field_measurements(field, page_group=page_group),
        ]
```

Add the `TYPE_CHECKING` import of `FieldSnapshot` at the top, as in Task 8.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/rag_test.py -v`
Expected: PASS — the new field tests plus every pre-existing prompt test, proving a field-less prompt is byte-identical to before.

- [ ] **Step 5: Commit**

```bash
git add rag/prompt.py tests/unit/rag_test.py
git commit -m "Ground analysis in real-user data as trusted measurement"
```

---

### Task 10: Report model — field blocks and `field_mode`

**Files:**
- Modify: `analysis/reportmodel.py` (add models, extend `build_report` and `_page_block`)
- Test: `tests/unit/reportmodel_test.py`

**Interfaces:**
- Consumes: `normalize.field.FieldSnapshot`.
- Produces: `FieldHeadline`, `FieldSegments`, `FieldBlock`, `PageFieldBlock`; `Report.field: FieldBlock`; `PageBlock.field: PageFieldBlock`; `ReportMeta.field_mode: str`; `build_report(..., field: Optional[FieldSnapshot] = None)`; `analysis.reportmodel.field_mode_for(snapshot, *, generated_at, window) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/reportmodel_test.py`:

```python
def test_field_mode_is_unavailable_without_a_snapshot():
    from analysis.reportmodel import field_mode_for

    assert field_mode_for(None, generated_at=None, window="7d") == "unavailable"


def test_field_mode_is_live_inside_one_window():
    from datetime import datetime, timedelta, timezone

    from analysis.reportmodel import field_mode_for
    from normalize.field import FieldSnapshot

    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    snap = FieldSnapshot(
        snapshot_id="s", project="p", hosts=["a.com"],
        window_from=now - timedelta(days=7), window_to=now, fetched_at=now,
    )
    assert field_mode_for(snap, generated_at=now, window="7d") == "live"


def test_field_mode_is_stale_beyond_one_window():
    """A 7d snapshot fetched over 7 days ago no longer overlaps the period it
    is being read against."""
    from datetime import datetime, timedelta, timezone

    from analysis.reportmodel import field_mode_for
    from normalize.field import FieldSnapshot

    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    old = now - timedelta(days=20)
    snap = FieldSnapshot(
        snapshot_id="s", project="p", hosts=["a.com"],
        window_from=old - timedelta(days=7), window_to=old, fetched_at=old,
    )
    assert field_mode_for(snap, generated_at=now, window="7d") == "stale"


def test_report_without_field_data_still_carries_an_empty_field_block(
    sample_analyses, sample_settings
):
    """Absence is a state, never an omitted section."""
    from datetime import datetime, timezone

    from analysis.reportmodel import build_report

    report = build_report(
        sample_analyses, project="p", settings=sample_settings,
        summary=_summary(), generated_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        model="none",
    )
    assert report.field is not None
    assert report.field.available is False
    assert report.meta.field_mode == "unavailable"
    assert all(page.field is not None for page in report.pages)


def test_page_field_block_joins_through_the_configured_map(
    sample_analyses, sample_settings
):
    from datetime import datetime, timezone

    from analysis.reportmodel import build_report
    from normalize.field import FieldSnapshot, PageTypeRow

    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    page_name = sample_analyses[0].page_name
    sample_settings.grafana.page_groups = {"Pdp": page_name}
    snapshot = FieldSnapshot(
        snapshot_id="s", project="p", hosts=["a.com"],
        window_from=now, window_to=now, fetched_at=now,
        by_pagetype=[PageTypeRow(page_group="Pdp", lcp_p75=4100.0, bounce_pct=61.5)],
    )

    report = build_report(
        sample_analyses, project="p", settings=sample_settings,
        summary=_summary(), generated_at=now, model="none", field=snapshot,
    )
    joined = next(p for p in report.pages if p.name == page_name)
    assert joined.field.available is True
    assert joined.field.lcp_p75 == 4100.0
    assert joined.field.page_group == "Pdp"


def test_unmapped_page_renders_the_not_available_state(
    sample_analyses, sample_settings
):
    from datetime import datetime, timezone

    from analysis.reportmodel import build_report
    from normalize.field import FieldSnapshot

    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    sample_settings.grafana.page_groups = {}
    snapshot = FieldSnapshot(
        snapshot_id="s", project="p", hosts=["a.com"],
        window_from=now, window_to=now, fetched_at=now,
    )
    report = build_report(
        sample_analyses, project="p", settings=sample_settings,
        summary=_summary(), generated_at=now, model="none", field=snapshot,
    )
    assert all(p.field.available is False for p in report.pages)
```

> Reuse the fixtures/helpers `tests/unit/reportmodel_test.py` already defines (`sample_analyses`, `sample_settings`, `_summary`). Read the file first and match its actual names.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/reportmodel_test.py -k field -v`
Expected: FAIL — `ImportError: cannot import name 'field_mode_for'`.

- [ ] **Step 3: Write minimal implementation**

In `analysis/reportmodel.py`, add before `class Report`:

```python
class FieldHeadline(BaseModel):
    """The brand-wide real-user figures, printed against their targets."""

    sessions: Optional[int] = None
    bounce_pct: Optional[float] = None
    conversion_pct: Optional[float] = None
    avg_session_pages: Optional[float] = None
    lcp_p75: Optional[float] = None
    inp_p75: Optional[float] = None
    cls_p75: Optional[float] = None
    ttfb_p75: Optional[float] = None
    rage_clicks_total: Optional[int] = None
    frustration_p75: Optional[float] = None


class FieldSegments(BaseModel):
    """The breakdown tables, carried through verbatim."""

    by_device: List[Dict[str, Any]] = Field(default_factory=list)
    by_country: List[Dict[str, Any]] = Field(default_factory=list)
    by_pagetype: List[Dict[str, Any]] = Field(default_factory=list)
    inp_buckets: List[Dict[str, Any]] = Field(default_factory=list)
    assets: List[Dict[str, Any]] = Field(default_factory=list)


class FieldBlock(BaseModel):
    """The report's field section.

    ``available`` false is a rendered *state*, not an omission: a section that
    disappears when Grafana is unreachable is exactly the drift
    ``--skeleton-check`` exists to catch.
    """

    available: bool = False
    mode: str = "unavailable"
    window_from: Optional[datetime] = None
    window_to: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    hosts: List[str] = Field(default_factory=list)
    headline: FieldHeadline = Field(default_factory=FieldHeadline)
    segments: FieldSegments = Field(default_factory=FieldSegments)
    symptoms: List[SymptomModel] = Field(default_factory=list)
    #: The session KPI series, carried through for the chart in Task 12.
    #: Dumped rather than typed because the report model is a serialisation
    #: boundary — the chart reads it as plain data.
    series: List[Dict[str, Any]] = Field(default_factory=list)


class PageFieldBlock(BaseModel):
    """One page's real-user counterpart, joined via ``grafana.page_groups``."""

    available: bool = False
    page_group: Optional[str] = None
    beacons: Optional[int] = None
    lcp_p75: Optional[float] = None
    inp_p75: Optional[float] = None
    cls_p75: Optional[float] = None
    bounce_pct: Optional[float] = None
    frustration_p75: Optional[float] = None
    rage_clicks: Optional[int] = None
```

Add to `PageBlock`:

```python
    #: Defaulted so a report.json written before this existed still validates.
    field: PageFieldBlock = Field(default_factory=PageFieldBlock)
```

Add to `Report` (after `summary`):

```python
    field: FieldBlock = Field(default_factory=FieldBlock)
```

Add to `ReportMeta`:

```python
    #: "live" | "stale" | "unavailable" — see field_mode_for.
    field_mode: str = "unavailable"
```

Add the mode helper and builders:

```python
def field_mode_for(
    snapshot: Optional[Any], *, generated_at: Optional[datetime], window: str
) -> str:
    """Whether the snapshot still describes the period it is read against.

    Stale means ``window_to`` precedes ``generated_at`` by more than one window
    length: a 7d snapshot fetched over 7 days ago no longer overlaps. It still
    renders, with its fetch date stated, rather than silently handing the
    reader last month's numbers.
    """
    if snapshot is None:
        return "unavailable"
    if generated_at is None:
        return "live"
    from ingest.field import window_delta

    try:
        span = window_delta(window)
    except ValueError:
        return "live"
    return "stale" if (generated_at - snapshot.window_to) > span else "live"


def _field_block(
    snapshot: Optional[Any], settings: Settings, generated_at: Optional[datetime]
) -> FieldBlock:
    mode = field_mode_for(
        snapshot, generated_at=generated_at, window=settings.grafana.window
    )
    if snapshot is None:
        return FieldBlock(available=False, mode=mode)

    from rag.retrieve import detect_field_symptoms

    return FieldBlock(
        available=True,
        mode=mode,
        window_from=snapshot.window_from,
        window_to=snapshot.window_to,
        fetched_at=snapshot.fetched_at,
        hosts=list(snapshot.hosts),
        headline=FieldHeadline(
            sessions=snapshot.sessions.session_count,
            bounce_pct=snapshot.sessions.bounce_pct,
            conversion_pct=snapshot.sessions.conversion_pct,
            avg_session_pages=snapshot.sessions.avg_session_pages,
            lcp_p75=snapshot.vitals.lcp_p75,
            inp_p75=snapshot.vitals.inp_p75,
            cls_p75=snapshot.vitals.cls_p75,
            ttfb_p75=snapshot.vitals.ttfb_p75,
            rage_clicks_total=snapshot.frustration.rage_clicks_total,
            frustration_p75=snapshot.frustration.frustration_p75,
        ),
        series=[p.model_dump(mode="json") for p in snapshot.sessions.series],
        segments=FieldSegments(
            by_device=[r.model_dump(mode="json") for r in snapshot.by_device],
            by_country=[r.model_dump(mode="json") for r in snapshot.by_country],
            by_pagetype=[r.model_dump(mode="json") for r in snapshot.by_pagetype],
            inp_buckets=[r.model_dump(mode="json") for r in snapshot.inp_buckets],
            assets=[r.model_dump(mode="json") for r in snapshot.assets],
        ),
        symptoms=[
            SymptomModel(code=s.code, text=s.text, severity=s.severity,
                         metric=s.metric, value=s.value, target=s.target)
            for s in detect_field_symptoms(snapshot, thresholds=settings.thresholds)
        ],
    )


def _page_field_block(
    snapshot: Optional[Any], settings: Settings, page_name: str
) -> PageFieldBlock:
    """Join one lab page to its mPulse page group, or say it is not mapped."""
    if snapshot is None:
        return PageFieldBlock(available=False)
    group = next(
        (g for g, name in settings.grafana.page_groups.items() if name == page_name),
        None,
    )
    row = snapshot.page_row(group) if group else None
    if row is None:
        return PageFieldBlock(available=False, page_group=group)
    return PageFieldBlock(
        available=True, page_group=group, beacons=row.beacons,
        lcp_p75=row.lcp_p75, inp_p75=row.inp_p75, cls_p75=row.cls_p75,
        bounce_pct=row.bounce_pct, frustration_p75=row.frustration_p75,
        rage_clicks=row.rage_clicks,
    )
```

Extend `build_report` with `field: Optional[Any] = None`, pass it into `_page_block` (which gains a `field: PageFieldBlock` parameter it sets on the returned `PageBlock`), and set `field=_field_block(field, settings, generated_at)` plus `field_mode=...` on `ReportMeta`:

```python
    field_block = _field_block(field, settings, generated_at)
    page_blocks = [
        _page_block(p, settings, trends.get(p.page_name, ()),
                    field=_page_field_block(field, settings, p.page_name))
        for p in ordered
    ]
```

and in `ReportMeta(...)`: `field_mode=field_block.mode,`.

Bump `SCHEMA_VERSION` by one — the Report JSON gained top-level `field`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/reportmodel_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add analysis/reportmodel.py tests/unit/reportmodel_test.py
git commit -m "Model the field section and the per-page field join"
```

---

### Task 11: Analysis wiring — load the snapshot, thread it through

**Files:**
- Modify: `analysis/__main__.py` (`run_analysis`, `_build_parser`, `main`)
- Modify: `analysis/findings.py` (pass `field`/`page_group` to the prompt)
- Test: `tests/integration/analysis_pipeline_test.py`

**Interfaces:**
- Consumes: Tasks 8–10.
- Produces: `run_analysis(..., field: Optional[FieldSnapshot] = None)`; `--no-field` flag; `analysis.__main__.load_field_snapshot(settings, project) -> Optional[FieldSnapshot]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/analysis_pipeline_test.py`:

```python
def test_analysis_succeeds_with_no_snapshot_in_the_store(tmp_path, sample_runs):
    """Analysis never fails because Grafana was down."""
    from analysis.__main__ import run_analysis

    report = run_analysis(sample_runs, llm_disabled=True, history=[])
    assert report.meta.field_mode == "unavailable"
    assert report.field.available is False


def test_analysis_uses_the_latest_stored_snapshot(tmp_path, sample_runs):
    from datetime import datetime, timezone

    from analysis.__main__ import load_field_snapshot
    from config.load import Settings
    from normalize.field import FieldSnapshot, SessionKpis
    from store import sql

    store_path = tmp_path / "runs.sqlite"
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    conn = sql.connect(store_path)
    sql.init_schema(conn)
    sql.insert_snapshot(conn, FieldSnapshot(
        snapshot_id="s1", project=sample_runs[0].project.name,
        hosts=["a.com"], window_from=now, window_to=now, fetched_at=now,
        sessions=SessionKpis(bounce_pct=44.0),
    ))
    conn.close()

    settings = Settings(storage={"sqlite_path": str(store_path)})
    loaded = load_field_snapshot(settings, sample_runs[0].project.name)
    assert loaded.sessions.bounce_pct == 44.0


def test_load_field_snapshot_returns_none_when_the_store_is_absent(tmp_path):
    from analysis.__main__ import load_field_snapshot
    from config.load import Settings

    settings = Settings(storage={"sqlite_path": str(tmp_path / "missing.sqlite")})
    assert load_field_snapshot(settings, "oakley") is None
    assert not (tmp_path / "missing.sqlite").exists(), (
        "must not create the store as a side effect"
    )


def test_no_field_flag_suppresses_a_stored_snapshot(sample_runs):
    from analysis.__main__ import _build_parser

    args = _build_parser().parse_args(["--no-field"])
    assert args.no_field is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/analysis_pipeline_test.py -k field -v`
Expected: FAIL — `ImportError: cannot import name 'load_field_snapshot'`.

- [ ] **Step 3: Write minimal implementation**

In `analysis/__main__.py`, add:

```python
def load_field_snapshot(settings: Any, project: str) -> Optional[Any]:
    """The latest stored field snapshot for a project, or None.

    Never creates the store: ``sql.connect`` creates what it opens, and a
    campaign analysed from a directory would otherwise leave an empty database
    behind just by asking whether field data exists.
    """
    path = Path(settings.storage.sqlite_path)
    if not path.is_file():
        return None
    from store import sql

    conn = sql.connect(path)
    try:
        sql.init_schema(conn)
        return sql.get_latest_snapshot(conn, project)
    except Exception as exc:  # field data must never cost a report
        print(f"Field data unavailable: {exc}", file=sys.stderr)
        return None
    finally:
        conn.close()
```

Extend `run_analysis` with `field: Optional[Any] = None`, resolve it when not injected — after `project` is computed and before `build_report`:

```python
    if field is None and not no_field:
        field = load_field_snapshot(settings, project)
```

(add `no_field: bool = False` to the signature), pass `field=field` into `build_report`, and pass the page's snapshot into `analyze_page` so the prompt sees it:

```python
        page_group = next(
            (g for g, name in settings.grafana.page_groups.items()
             if name == _page_name),
            None,
        )
        analyses.append(analyze_page(
            page_runs, hits=hits, symptoms=symptoms, client=page_client,
            prior_findings=priors, chunks=chunks,
            no_client_reason=page_reason,
            field=field, page_group=page_group,
        ))
```

Note this requires moving the `field` resolution **above** the per-page loop; compute `project` from `runs[0].project.name` at the top of the function rather than just before `build_report`.

In `analysis/findings.py`, extend `analyze_page` with `field=None, page_group=None` and forward both to `build_analysis_prompt`. Also extend the symptom list the page carries:

```python
    if field is not None:
        from rag.retrieve import detect_field_symptoms

        symptoms = list(symptoms) + detect_field_symptoms(
            field, page_group=page_group, thresholds=thresholds
        )
```

placing it where `symptoms` is already available; if `analyze_page` has no `thresholds` parameter, pass `None` and let the default apply.

Add the CLI flag in `_build_parser`:

```python
    p.add_argument("--no-field", action="store_true",
                   help="Ignore any stored field data; analyse lab metrics only.")
```

and thread `no_field=args.no_field` through `main` into `run_analysis`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/analysis_pipeline_test.py tests/unit/findings_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add analysis/__main__.py analysis/findings.py tests/integration/analysis_pipeline_test.py
git commit -m "Thread the stored field snapshot through analysis"
```

---

### Task 12: Templates, charts, and the skeleton baseline

**Files:**
- Modify: `report/template/report.html.j2`, `report/template/report.md.j2`, `report/template/style.css`
- Modify: `report/charts.py`
- Modify: `report/skeleton.baseline.json` (via `--update-baseline`, never by hand)
- Test: `tests/unit/skeleton_test.py`, `tests/unit/charts_test.py`, `tests/unit/render_html_test.py`

**Interfaces:**
- Consumes: Task 10's `FieldBlock` / `PageFieldBlock`.
- Produces: five new `data-section` values; `report.charts.field_sessions_chart(block) -> Optional[str]`, `report.charts.lab_vs_field_chart(page) -> Optional[str]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/skeleton_test.py`:

```python
def test_baseline_contains_the_field_sections():
    from report.skeleton import load_baseline

    sections = load_baseline()
    for name in ("field", "field.headline", "field.frustration",
                 "field.segments", "field.assets", "page.field"):
        assert name in sections, name


def test_field_sits_after_plan_and_before_the_page_group():
    from report.skeleton import load_baseline

    sections = load_baseline()
    assert sections.index("plan") < sections.index("field")
    assert sections.index("field") < sections.index("page[]")


def test_page_field_sits_inside_the_page_group():
    from report.skeleton import load_baseline

    sections = load_baseline()
    assert sections.index("page.at-a-glance") < sections.index("page.field")
    assert sections.index("page.field") < sections.index("page.findings")
```

Append to `tests/unit/render_html_test.py`:

```python
def test_field_section_renders_when_unavailable(minimal_report):
    """Absence is a state; the section must still be in the document."""
    from report.render_html import render_html

    html = render_html(minimal_report)
    assert 'data-section="field"' in html
    assert "No field data" in html


def test_field_section_renders_the_headline_when_available(minimal_report):
    from analysis.reportmodel import FieldBlock, FieldHeadline
    from report.render_html import render_html

    minimal_report.field = FieldBlock(
        available=True, mode="live",
        headline=FieldHeadline(bounce_pct=41.0, conversion_pct=2.4),
    )
    html = render_html(minimal_report)
    assert "41" in html and "2.4" in html


def test_stale_snapshot_says_so(minimal_report):
    from analysis.reportmodel import FieldBlock
    from report.render_html import render_html

    minimal_report.field = FieldBlock(available=True, mode="stale")
    assert "stale" in render_html(minimal_report).lower()
```

Append to `tests/unit/charts_test.py`:

```python
def test_field_sessions_chart_is_empty_without_a_series():
    from report.charts import NO_CHART, field_sessions_chart

    assert field_sessions_chart([]) == NO_CHART


def test_field_sessions_chart_needs_more_than_one_point():
    from report.charts import NO_CHART, field_sessions_chart

    one = [{"time": "2026-08-24T00:00:00+00:00", "values": {"bounce_pct": 40.0}}]
    assert field_sessions_chart(one) == NO_CHART


def test_field_sessions_chart_accepts_iso_strings_from_reloaded_json():
    from report.charts import NO_CHART, field_sessions_chart

    series = [
        {"time": "2026-08-23T00:00:00+00:00", "values": {"bounce_pct": 40.0}},
        {"time": "2026-08-24T00:00:00+00:00", "values": {"bounce_pct": 44.0}},
    ]
    assert field_sessions_chart(series) != NO_CHART


def test_lab_vs_field_chart_is_empty_when_one_side_is_missing():
    """A bar with nothing to compare against invites comparison with zero."""
    from report.charts import NO_CHART, lab_vs_field_chart

    assert lab_vs_field_chart({"lcp_ms": 4000.0}, {"lcp_p75": None}) == NO_CHART
    assert lab_vs_field_chart({}, {"lcp_p75": 4100.0}) == NO_CHART


def test_lab_vs_field_chart_draws_when_both_sides_are_present():
    from report.charts import NO_CHART, lab_vs_field_chart

    svg = lab_vs_field_chart({"lcp_ms": 4000.0}, {"lcp_p75": 4100.0})
    assert svg != NO_CHART and svg.startswith("<svg")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/skeleton_test.py tests/unit/render_html_test.py tests/unit/charts_test.py -v`
Expected: FAIL — baseline lacks `field`; `render_html` produces no `data-section="field"`.

- [ ] **Step 3: Write minimal implementation**

**House rule this task must respect:** `report/render_html.py:117` states *"Jinja is deliberately kept free of arithmetic: the same rows have to appear in the Markdown mirror, and two templates computing them separately is how the two documents drift apart."* So every derived value is computed **once in Python** and passed to both templates, exactly as `glance_by_page` already is.

**3a. Add the row builders** to `report/render_html.py`, beside `glance_by_page`:

```python
def _fmt(value: Optional[float], unit: str = "", decimals: int = 0) -> str:
    """A metric cell, or an em dash. Never prints 0 for an absent value."""
    if value is None:
        return "—"
    return f"{value:.{decimals}f}{unit}"


def field_headline_rows(report) -> list:
    """The brand-wide figures as label/value pairs, computed once."""
    h = report.field.headline
    return [
        {"label": "Sessions", "value": _fmt(h.sessions)},
        {"label": "Bounce rate", "value": _fmt(h.bounce_pct, "%", 1)},
        {"label": "Conversion rate", "value": _fmt(h.conversion_pct, "%", 2)},
        {"label": "Pages per session", "value": _fmt(h.avg_session_pages, "", 2)},
        {"label": "LCP p75", "value": _fmt(h.lcp_p75, "ms")},
        {"label": "INP p75", "value": _fmt(h.inp_p75, "ms")},
        {"label": "CLS p75", "value": _fmt(h.cls_p75, "", 3)},
        {"label": "TTFB p75", "value": _fmt(h.ttfb_p75, "ms")},
        {"label": "Rage clicks", "value": _fmt(h.rage_clicks_total)},
        {"label": "Frustration index p75", "value": _fmt(h.frustration_p75, "", 1)},
    ]


def field_rows_by_page(report) -> Dict[str, list]:
    """Lab beside field, per page, keyed by page name.

    The comparison is the point of the whole feature, so it is built here
    rather than in two templates: an emulated number and a p75 field number
    formatted differently in HTML and Markdown would undermine the one row
    readers are meant to trust.
    """
    rows: Dict[str, list] = {}
    for page in report.pages:
        field = page.field
        # PageBlock.metrics is a plain dict (the Run's metrics dumped whole),
        # unpacked here rather than in the template - build_charts already
        # does exactly this at report/render_html.py:88, because the template
        # does not reach into structures.
        cwp = page.metrics.get("cwp", {}) if isinstance(page.metrics, dict) else {}
        rows[page.name] = [
            {"label": "LCP",
             "lab": _fmt(cwp.get("lcp_ms"), "ms"),
             "field": _fmt(field.lcp_p75, "ms")},
            {"label": "INP",
             "lab": _fmt(cwp.get("inp_ms"), "ms"),
             "field": _fmt(field.inp_p75, "ms")},
            {"label": "CLS",
             "lab": _fmt(cwp.get("cls"), "", 3),
             "field": _fmt(field.cls_p75, "", 3)},
            # No lab counterpart exists for these: a single scripted navigation
            # cannot bounce, get frustrated, or rage-click. The em dash says
            # "not measurable here", which is not the same as "zero".
            {"label": "Entry bounce rate",
             "lab": "—", "field": _fmt(field.bounce_pct, "%", 1)},
            {"label": "Frustration index p75",
             "lab": "—", "field": _fmt(field.frustration_p75, "", 1)},
            {"label": "Rage clicks",
             "lab": "—", "field": _fmt(field.rage_clicks)},
        ]
    return rows
```

Pass both into `template.render(...)` in `render_html`, and add the identical two arguments to the Markdown renderer in `report/render_md.py` so the mirror uses the same rows.

**3b. The HTML template.** In `report/template/report.html.j2`, insert after the `plan` section's closing `</section>` and before `{% for page in report.pages %}`:

```jinja
<section data-section="field" class="sheet">
  <p class="eyebrow">Real users</p>
  <h2>What visitors actually experienced</h2>
  <p class="lede">Measured from real sessions, not from the emulated campaign.
    Where these disagree with the page measurements below, these are what your
    visitors lived through.</p>
  {% if not report.field.available %}
  <p class="empty">No field data for this window. Run
    <code>python -m cli ingest field</code> to fetch it from Grafana.</p>
  {% elif report.field.mode == "stale" %}
  <p class="empty">These figures are stale — fetched
    {{ report.field.fetched_at.strftime("%Y-%m-%d") }}, covering
    {{ report.field.window_from.strftime("%Y-%m-%d") }} to
    {{ report.field.window_to.strftime("%Y-%m-%d") }}. Re-run
    <code>python -m cli ingest field</code> for current numbers.</p>
  {% else %}
  <p class="url mono">{{ report.field.window_from.strftime("%Y-%m-%d") }} to
    {{ report.field.window_to.strftime("%Y-%m-%d") }} ·
    {{ report.field.hosts | join(", ") }}</p>
  {% endif %}

  <div data-section="field.headline" class="block">
    <h3>Engagement and real-user vitals</h3>
    {% if report.field.available %}
    <table class="metrics glance">
      <thead><tr><th>Metric</th><th>Measured</th></tr></thead>
      <tbody>
      {% for row in field_headline %}
        <tr><td>{{ row.label }}</td><td>{{ row.value }}</td></tr>
      {% endfor %}
      </tbody>
    </table>
    {% if charts.field_sessions %}
    <div class="chart">{{ charts.field_sessions|safe }}</div>
    {% endif %}
    {% else %}
    <p class="empty">No engagement data.</p>
    {% endif %}
  </div>

  <div data-section="field.frustration" class="block">
    <h3>Where users got stuck</h3>
    {% if report.field.symptoms %}
    <ul class="findings">
      {% for symptom in report.field.symptoms %}
      <li><span class="tag tag--{{ symptom.severity }}">{{ symptom.severity }}</span>
        <span class="serif">{{ symptom.text }}</span></li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="empty">No frustration signals crossed their thresholds.</p>
    {% endif %}
    {% if report.field.segments.inp_buckets %}
    <table class="metrics">
      <thead><tr><th>Interaction speed</th><th>Beacons</th>
        <th>Avg frustration</th><th>% with rage clicks</th></tr></thead>
      <tbody>
      {% for row in report.field.segments.inp_buckets %}
        <tr><td>{{ row.bucket }}</td><td>{{ row.beacons }}</td>
          <td>{{ row.avg_frustration }}</td><td>{{ row.rage_session_pct }}</td></tr>
      {% endfor %}
      </tbody>
    </table>
    {% endif %}
  </div>

  <div data-section="field.segments" class="block">
    <h3>By device, market and page type</h3>
    {% for caption, key, label_col in [
        ("Device", "by_device", "device"),
        ("Market", "by_country", "country"),
        ("Page type", "by_pagetype", "page_group")] %}
      {% set rows = report.field.segments[key] %}
      <h4>{{ caption }}</h4>
      {% if rows %}
      <table class="metrics">
        <thead><tr><th>{{ caption }}</th><th>Beacons</th><th>LCP p75</th>
          <th>INP p75</th><th>CLS p75</th><th>Frustration p75</th></tr></thead>
        <tbody>
        {% for row in rows %}
          <tr><td>{{ row[label_col] }}</td><td>{{ row.beacons }}</td>
            <td>{{ row.lcp_p75 }}</td><td>{{ row.inp_p75 }}</td>
            <td>{{ row.cls_p75 }}</td><td>{{ row.frustration_p75 }}</td></tr>
        {% endfor %}
        </tbody>
      </table>
      {% else %}
      <p class="empty">No {{ caption | lower }} breakdown available.</p>
      {% endif %}
    {% endfor %}
  </div>

  <div data-section="field.assets" class="block">
    <h3>What the CDN served</h3>
    <p class="serif">A low cache-hit percentage means real users waited for the
      origin to answer for content that could have been served from the edge.</p>
    {% if report.field.segments.assets %}
    <table class="metrics">
      <thead><tr><th>Asset type</th><th>Requests</th><th>Avg size</th>
        <th>Edge time</th><th>Origin time</th><th>Cache hit</th></tr></thead>
      <tbody>
      {% for row in report.field.segments.assets %}
        <tr><td>{{ row.asset_type }}</td><td>{{ row.request_count }}</td>
          <td>{{ row.avg_size_kb }} KB</td><td>{{ row.edge_ms }} ms</td>
          <td>{{ row.origin_ms }} ms</td><td>{{ row.cache_hit_pct }}%</td></tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="empty">No CDN breakdown available.</p>
    {% endif %}
  </div>
</section>
```

**3c. The per-page block.** Inside the page loop, immediately after the `page.at-a-glance` `</div>`:

```jinja
  <div data-section="page.field" class="block">
    <h3>Real users on this page</h3>
    {% if not page.field.available %}
    <p class="empty">This page is not mapped to an mPulse page group. Add it to
      <code>grafana.page_groups</code> in settings.yaml to compare it against
      real-user data.</p>
    {% else %}
    <p class="serif">The campaign above emulates one device on one connection.
      This is the same page as {{ page.field.beacons }} real visits measured it
      ({{ page.field.page_group }}).</p>
    <table class="metrics glance">
      <thead><tr><th>Metric</th><th>This campaign</th><th>Real users (p75)</th></tr></thead>
      <tbody>
      {% for row in field_rows[page.name] %}
        <tr><td>{{ row.label }}</td><td>{{ row.lab }}</td><td>{{ row.field }}</td></tr>
      {% endfor %}
      </tbody>
    </table>
    {% if charts.pages[page.name].lab_vs_field %}
    <div class="chart">{{ charts.pages[page.name].lab_vs_field|safe }}</div>
    {% endif %}
    {% endif %}
  </div>
```

**3d. The Markdown mirror.** Add all five blocks to `report/template/report.md.j2` using the same `field_headline` and `field_rows` context variables and that file's existing heading conventions. Because both templates consume the *same* precomputed rows, the two documents cannot disagree about a number.

**3e. Charts.** Charts in this project are **inline SVG strings**, embedded with `|safe`, using `NO_CHART = ""` as the empty sentinel — not base64, not `None`. Follow that exactly.

Add to `report/charts.py`:

```python
def field_sessions_chart(series: Sequence[Mapping]) -> str:
    """Bounce and conversion over the window, on two axes.

    Two axes because the dashboard draws them that way and because the scales
    are incomparable: bounce sits near 40%, conversion near 2%, and one axis
    would flatten conversion into the baseline.

    Returns ``NO_CHART`` when there is nothing to plot — a chart of one point
    is less informative than the sentence the template prints instead.
    """
    points = [p for p in series if p.get("time") is not None]
    if len(points) < 2:
        return NO_CHART

    times = [p["time"] for p in points]
    bounce = [(p.get("values") or {}).get("bounce_pct") for p in points]
    conversion = [(p.get("values") or {}).get("conversion_pct") for p in points]
    if not any(v is not None for v in bounce):
        return NO_CHART

    fig, axis = plt.subplots(figsize=(9, 2.6))
    axis.plot(times, bounce, linewidth=2, color=PALETTE["warn"], label="Bounce %")
    axis.set_ylabel("Bounce %")
    axis.set_ylim(bottom=0)

    if any(v is not None for v in conversion):
        right = axis.twinx()
        right.plot(times, conversion, linewidth=2, color=PALETTE["pass"],
                   label="Conversion %")
        right.set_ylabel("Conversion %")
        right.set_ylim(bottom=0)

    _bare_axis(axis)
    fig.autofmt_xdate()
    return to_svg(fig)


def lab_vs_field_chart(
    cwp: Mapping[str, Optional[float]], page_field: Mapping
) -> str:
    """This campaign's Core Web Vitals beside the p75 real users get.

    Grouped bars on a shared axis per metric pair. Only metrics present on
    *both* sides are drawn: a bar with nothing to compare against invites the
    reader to compare it with zero.
    """
    pairs = [
        ("LCP", cwp.get("lcp_ms"), page_field.get("lcp_p75")),
        ("INP", cwp.get("inp_ms"), page_field.get("inp_p75")),
    ]
    present = [(l, a, b) for l, a, b in pairs if a is not None and b is not None]
    if not present:
        return NO_CHART

    fig, axis = plt.subplots(figsize=(6, 2.4))
    positions = range(len(present))
    width = 0.38
    axis.bar([p - width / 2 for p in positions], [p[1] for p in present],
             width, label="This campaign", color=PALETTE["neutral"])
    axis.bar([p + width / 2 for p in positions], [p[2] for p in present],
             width, label="Real users p75", color=PALETTE["accent"])
    axis.set_xticks(list(positions))
    axis.set_xticklabels([p[0] for p in present])
    axis.set_ylabel("ms")
    axis.legend(frameon=False, fontsize="small")
    _bare_axis(axis)
    return to_svg(fig)
```

> **Palette keys are illustrative.** `report/charts.py` imports from `report/palette.py`; read it and substitute the actual key or constant names for `PALETTE["warn"]`, `["pass"]`, `["neutral"]` and `["accent"]`. Do **not** add new colours — the whole point of that module is one palette.

In `report/render_html.py`'s `build_charts`, add to the returned dict:

```python
        "field_sessions": charts.field_sessions_chart(report.field.series),
```

and inside the existing per-page dict (`report/render_html.py:89`), reusing the `cwp` variable unpacked on the line above:

```python
            "lab_vs_field": charts.lab_vs_field_chart(
                cwp, page.field.model_dump()
            ),
```

`FieldBlock.series` was added in Task 10 and holds `snapshot.sessions.series` dumped to plain dicts, which is the shape `field_sessions_chart` expects.

Note that `field_sessions_chart` reads `p["time"]` as a **datetime**, but `model_dump(mode="json")` makes it an ISO string. Parse it in the chart with `datetime.fromisoformat` when it arrives as `str`, so the function works against both the live model and a report reloaded from JSON:

```python
    times = [
        datetime.fromisoformat(p["time"]) if isinstance(p["time"], str)
        else p["time"]
        for p in points
    ]
```

Then regenerate the baseline — **never hand-edit it**:

```bash
python -m cli report --skeleton-check   # confirm it FAILS, listing the 5 additions
python -m cli report --update-baseline
git diff report/skeleton.baseline.json  # confirm exactly 6 added lines, no removals
```

The diff must show `field`, `field.headline`, `field.frustration`, `field.segments`, `field.assets` and `page.field` added, and **nothing removed**. A removal means a section was displaced and must be fixed in the template, not accepted into the baseline.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/skeleton_test.py tests/unit/render_html_test.py tests/unit/render_md_test.py tests/unit/charts_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add report/template/ report/charts.py report/skeleton.baseline.json \
        tests/unit/skeleton_test.py tests/unit/render_html_test.py \
        tests/unit/charts_test.py
git commit -m "Render the field section and the per-page lab-vs-field row"
```

---

### Task 13: Documentation and full-suite verification

**Files:**
- Modify: `README.md`, `docs/PROJECT_SPEC.md`
- Test: the whole suite

**Interfaces:**
- Consumes: everything.
- Produces: nothing importable.

- [ ] **Step 1: Run the full suite and record the result**

Run: `python -m pytest -q`
Expected: PASS. Any failure here is a real regression from Tasks 1–12 — fix it before writing documentation that claims the feature works.

- [ ] **Step 2: Regenerate a real report end to end**

```bash
python -m cli ingest field --project oakley   # needs the four .env variables
python -m cli analyze --no-llm
python -m cli report --skeleton-check
```

Expected: the field section appears, populated. If `.env` is not yet filled in, `ingest field` names the missing variable and the remaining two commands still succeed with `field_mode: unavailable` — verify **that** path instead and note it.

- [ ] **Step 3: Update the README**

In "Where the project is", add to the working-today list:

> **real-user (RUM) data pulled from Grafana** — bounce, conversion, pages per session, rage clicks, frustration index, field Core Web Vitals and CDN cache behaviour, joined onto each tested page

Add `ingest field` to the command table, document the four `.env` variables with a pointer to `.env.example`, and state the two honest limitations:

> - **Field bounce per page group is entry-page bounce** — sessions that *started* on that page group and viewed one page. It is not the same quantity as the site-wide bounce rate, and the two should not be subtracted from one another casually.
> - **The `pageGroupName` → page-name map is configured, not inferred.** An unmapped page prints its field row as unavailable rather than guessing.

- [ ] **Step 4: Update PROJECT_SPEC**

Add a section describing the third ingestion door and the `FieldSnapshot` entity, cross-referencing the design doc.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/PROJECT_SPEC.md
git commit -m "Document Grafana field ingestion and its two known limits"
```

---

## Self-Review

**Spec coverage:** §1 config → Task 1. §2 host derivation → Task 7 (`hosts_for`). §3.1 client → Task 4. §3.2 queries → Task 3. §3.3 host validation → Task 3. §3.4 parsing → Task 5. §4 model → Task 2. §5 persistence → Task 6, stage → Task 7. §6.1 RAG → Tasks 8 (symptoms) and 9 (prompt). §6.2 page join → Task 10. §7 skeleton and templates → Task 12. §8 failure behaviour → Tasks 7 (`ingest field` exits 1), 10 (`field_mode`), 11 (`analysis` never fails), 12 (always-rendered section). §9 testing → every task. §10 files/docs → Task 13. **No gaps.**

**Type consistency:** `FieldSnapshot.page_row()` defined Task 2, used Tasks 5, 8, 9, 10. `PageTypeRow.page_group` consistent throughout. `detect_field_symptoms(snapshot, *, page_group, thresholds)` defined Task 8, called Tasks 10 and 11 with the same keywords. `window_delta` defined Task 7, imported Task 10. `frame_rows`/`build_snapshot` defined Task 5, used Task 7. `GrafanaEnv.table` set Task 4, read Task 7.

**Known soft spots, flagged rather than hidden:**
1. **Task 3's brace escaping** is the fiddliest code in the plan — f-strings building `.format()` templates. `test_no_dashboard_variable_survives_rendering` is the guard; run it first.
2. **Task 12 obeys a house rule that is easy to miss.** `report/render_html.py:117` requires derived values to be computed in Python, not Jinja, so the Markdown mirror cannot drift. The task therefore adds `field_headline_rows` and `field_rows_by_page` beside `glance_by_page` and feeds both templates the same rows. An implementer who computes formatting inside the template will produce two documents that disagree about a number.
3. **Tasks 9, 10, 11, 12 reuse existing fixtures and attributes** whose exact names must be confirmed by reading the relevant file first — `sample_run` in `rag_test.py`, `sample_analyses`/`sample_settings` in `reportmodel_test.py`, `PageBlock.conditions` in `reportmodel.py:154`. Each such step says so inline.
4. **`SCHEMA_VERSION` is bumped in Task 10.** Check whether any test or fixture pins the old value before committing that task.
