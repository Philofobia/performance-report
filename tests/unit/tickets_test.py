"""Unit tests for analysis/tickets.py — the catalog and its evaluation."""
from __future__ import annotations

from pathlib import Path

import pytest

from analysis import har_checks as hc
from analysis import tickets as tk
from tests.unit.har_checks_test import HERO, _hero_waits_for_script, entry, page, three_runs


def catalog(*tickets, site_hosts=None):
    return tk.TicketCatalog(site_hosts=site_hosts or {"OO": ["www.oakley.com"]},
                            tickets=[tk.TicketSpec(**t) for t in tickets])


def test_the_committed_catalog_loads_and_every_ticket_is_answerable_or_explained():
    loaded = tk.load_catalog()

    assert loaded.tickets
    for ticket in loaded.tickets:
        assert ticket.check in hc.CHECKS or ticket.reason, ticket.id


def test_an_unknown_check_is_an_error(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("tickets:\n  - {id: X-1, title: t, check: no_such_check}\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="unknown check"):
        tk.load_catalog(path)


def test_a_ticket_with_no_check_must_say_why(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("tickets:\n  - {id: X-1, title: t}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reason"):
        tk.load_catalog(path)


def test_a_confirmed_ticket_names_the_capture_and_its_evidence(tmp_path):
    captures = [tk.Capture("homepage", "mobile", three_runs(tmp_path, _hero_waits_for_script))]
    cat = catalog({"id": "OAK-39155", "title": "hero hidden", "pages": ["homepage"],
                   "check": "lcp_render_delay", "params": {"script": "newHeroBanner.js"}})

    (outcome,) = tk.evaluate(cat, captures)

    assert outcome.status == hc.CONFIRMED
    assert outcome.summary == "Confirmed on homepage/mobile (1 of 1 captures)."
    assert outcome.observed_on == ["OO"]
    assert any("newHeroBanner.js finished at 1763 ms" in line for line in outcome.evidence)


def test_a_ticket_only_looks_at_its_own_pages(tmp_path):
    captures = [tk.Capture("homepage", "mobile", three_runs(tmp_path, _hero_waits_for_script))]
    cat = catalog({"id": "OAK-39157", "title": "pdp image", "pages": ["pdp"],
                   "check": "lcp_render_delay"})

    (outcome,) = tk.evaluate(cat, captures)

    assert outcome.status == hc.NO_DATA
    assert "pdp" in outcome.summary


def test_a_ticket_no_har_can_answer_is_reported_with_its_reason():
    cat = catalog({"id": "OAK-39153", "title": "do not sell", "reason": "Needs a click."})

    (outcome,) = tk.evaluate(cat, [])

    assert outcome.status == tk.NOT_CHECKABLE
    assert outcome.summary == "Needs a click."


def test_not_seen_when_every_capture_answers_no(tmp_path):
    def prompt(pid):
        return page(pid, lcp=1000, lcp_url=HERO), [entry(HERO, 700, 950, page=pid)]

    captures = [tk.Capture("homepage", "desktop", three_runs(tmp_path, prompt))]
    cat = catalog({"id": "T-1", "title": "t", "check": "lcp_render_delay"})

    (outcome,) = tk.evaluate(cat, captures)

    assert outcome.status == hc.NOT_SEEN


def test_every_ticket_is_reported_in_catalog_order(tmp_path):
    cat = catalog({"id": "A", "title": "a", "reason": "r"},
                  {"id": "B", "title": "b", "check": "service_worker"},
                  {"id": "C", "title": "c", "reason": "r"})

    assert [o.id for o in tk.evaluate(cat, [])] == ["A", "B", "C"]


def test_har_argument_parsing():
    assert tk.parse_har_arg("homepage/mobile=C:\\x\\HOMEMOB.har") == (
        "homepage", "mobile", Path("C:\\x\\HOMEMOB.har"))
    for bad in ("homepage=x.har", "homepage/mobile", "/mobile=x.har"):
        with pytest.raises(ValueError):
            tk.parse_har_arg(bad)
