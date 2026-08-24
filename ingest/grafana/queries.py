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
    # str satisfies Sequence[str] structurally, so a caller who passes a bare
    # hostname string instead of a one-element list would otherwise iterate
    # its characters - "abc" becoming the hosts ['a', 'b', 'c'] - silently
    # building nonsense SQL instead of failing loudly.
    if isinstance(hosts, str):
        raise HostError(
            f"hosts must be a sequence of hostnames, not a bare string: {hosts!r}"
        )
    cleaned = [h.strip() for h in hosts]
    if not cleaned:
        raise HostError(
            "No hosts to query. config/targets.yaml must list at least one "
            "page whose URL has a hostname."
        )
    for host in cleaned:
        if not _HOSTNAME.fullmatch(host):
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
