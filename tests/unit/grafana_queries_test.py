"""SQL rendering — the injection surface and the variable-substitution trap."""
import re

import pytest

from ingest.grafana.queries import (
    _HOSTNAME,
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
    "a.com\ndrop",
    "a.com\r--",
])
def test_hostile_hostnames_are_rejected_not_escaped(bad):
    """targets.yaml is operator-authored: a quote in a hostname is a mistake
    or an attack, and neither should reach ClickHouse."""
    with pytest.raises(HostError):
        quote_hosts([bad])


def test_empty_host_list_is_rejected():
    with pytest.raises(HostError):
        quote_hosts([])


def test_hostname_regex_is_fully_anchored():
    """Regression guard for a `$`-vs-`\\Z` anchoring bug: `re.match` with a
    trailing `$` also matches just before a trailing newline, so
    `_HOSTNAME.match("a.com\\n")` used to return a match even though the
    raw string was not a plain hostname. `quote_hosts` strips its input
    before validating, so a bare trailing newline was never exploitable
    end-to-end (`"a.com\\n".strip() == "a.com"`, a legitimately valid host)
    - but the regex itself must reject it too, or a future refactor that
    validates raw (unstripped) input reopens a newline-injection path into
    the SQL string. Tested directly against `_HOSTNAME` because the
    trailing-newline case is invisible at the `quote_hosts` boundary once
    `.strip()` has already run.
    """
    assert _HOSTNAME.fullmatch("a.com\n") is None
    assert _HOSTNAME.fullmatch("a.com\r") is None


def test_bare_string_is_rejected_not_iterated():
    """str satisfies Sequence[str] structurally: quote_hosts("abc") would
    otherwise silently iterate to the hosts ['a', 'b', 'c'] instead of
    raising, and build nonsense SQL from a caller's typo."""
    with pytest.raises(HostError):
        quote_hosts("www.oakley.com")


def test_cls_quantiles_are_divided_by_1000():
    """mPulse stores cumulativeLayoutShift multiplied by 1000; every panel
    in the source dashboard divides at query time. An implementation that
    dropped the divisor would still pass every other test here while
    silently reporting CLS 1000x too large."""
    sql_by_ref = render_all(table=TABLE, hosts=HOSTS)
    cls_quantile = re.compile(
        r"quantileIf\(0\.\d+\)\(cumulativeLayoutShift,[^)]*\)(\s*/\s*1000\.0)?"
    )
    # vitals selects CLS p75 *and* p95 directly; by_device, by_country, and
    # by_pagetype all get CLS via the shared _CWV_COLUMNS fragment (p75 only).
    expected_occurrences = {
        "vitals": 2, "by_device": 1, "by_country": 1, "by_pagetype": 1,
    }
    for ref_id, expected_count in expected_occurrences.items():
        matches = cls_quantile.findall(sql_by_ref[ref_id])
        assert len(matches) == expected_count, (
            f"{ref_id}: expected {expected_count} CLS quantile expression(s), "
            f"found {len(matches)}"
        )
        for divisor in matches:
            assert divisor.strip() == "/ 1000.0", (
                f"{ref_id}: CLS quantile is missing its /1000.0 divisor - "
                "every CLS value in the report would be 1000x too large"
            )


def test_unknown_ref_id_is_rejected():
    with pytest.raises(KeyError):
        render("not_a_panel", table=TABLE, hosts=HOSTS)


def test_by_pagetype_selects_the_columns_the_join_needs():
    sql = render("by_pagetype", table=TABLE, hosts=HOSTS)
    for column in ("page_group", "lcp_p75", "inp_p75", "frustration_p75",
                   "rage_clicks", "bounce_pct"):
        assert re.search(rf"\b{column}\b", sql), column
