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
