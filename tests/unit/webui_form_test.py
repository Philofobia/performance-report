"""Unit tests for webui/form.py — the form's field contract.

This module is the only place in the UI that touches raw strings, so it is the
only place that can get coercion wrong. The property that matters is the one
the run listing already depends on: a blank field is an absent measurement, not
a zero. A UI that turned an untouched LCP box into `0` would report the slowest
page in the campaign as the fastest.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from normalize.schema import LighthouseScores, Run
from webui import form


def submitted(**overrides):
    """A complete, valid submission; override one key per test."""
    values = {f.name: "" for f in form.FIELDS}
    values.update({
        "project": "storefront",
        "page": "homepage",
        "page_url": "https://example.com/",
        "device": "mid-mobile",
        "network": "slow-4g",
        "problem": "Homepage LCP spikes on 3G",
        "lcp_ms": "6200",
        "cls": "0.42",
    })
    values.update(overrides)
    return values


# --- coercion ---------------------------------------------------------------


def test_blank_numeric_is_absent_not_zero():
    kwargs, errors = form.parse(submitted(inp_ms=""))
    assert errors == {}
    assert kwargs["cwp"]["inp_ms"] is None


def test_a_measured_zero_survives_as_zero():
    kwargs, _ = form.parse(submitted(cls="0"))
    assert kwargs["cwp"]["cls"] == 0.0


def test_surrounding_whitespace_is_stripped():
    kwargs, _ = form.parse(submitted(lcp_ms="  6200 "))
    assert kwargs["cwp"]["lcp_ms"] == 6200.0


def test_non_numeric_input_is_a_field_error_naming_the_label():
    _, errors = form.parse(submitted(lcp_ms="abc"))
    assert "lcp_ms" in errors
    assert "LCP" in errors["lcp_ms"]


def test_whole_number_fields_reject_a_decimal_with_a_specific_message():
    _, errors = form.parse(submitted(runs="2.5"))
    assert "whole number" in errors["runs"]


def test_keywords_split_on_commas_and_blank_means_auto_derive():
    kwargs, _ = form.parse(submitted(keywords="lcp, hero , "))
    assert kwargs["keywords"] == ["lcp", "hero"]
    assert form.parse(submitted(keywords=""))[0]["keywords"] is None


def test_blank_context_fields_fall_through_to_the_cli_defaults():
    """An omitted key lets build_manual_run apply its own default."""
    kwargs, _ = form.parse(submitted(project="", device=""))
    assert "project" not in kwargs
    assert "device" not in kwargs


def test_parse_output_is_accepted_by_build_manual_run():
    from ingest.manual import build_manual_run
    kwargs, errors = form.parse(submitted())
    assert errors == {}
    run = build_manual_run(**kwargs)
    assert isinstance(run, Run)
    assert run.meta.runner == form.RUNNER


# --- schema-derived constraints ---------------------------------------------


def test_constraints_are_read_from_the_schema_not_hardcoded():
    """No `max` for CLS: the schema has no upper bound, so the markup has none.

    This is the property the generation exists for — dropping `le=1` from
    `CwpMetrics.cls` changed the rendered input without anyone editing the
    template, which a hardcoded `max="1"` would have silently contradicted.
    """
    cls_field = next(f for f in form.FIELDS if f.name == "cls")
    assert form.constraints(cls_field) == {"min": 0, "step": "any"}


def test_lighthouse_constraints_track_the_schema():
    perf = next(f for f in form.FIELDS if f.name == "performance")
    limits = form.constraints(perf)
    assert limits["max"] == LighthouseScores.model_fields["performance"].metadata[-1].le


def test_fields_without_a_schema_constraint_return_nothing():
    problem = next(f for f in form.FIELDS if f.name == "problem")
    assert form.constraints(problem) == {}


# --- error mapping ----------------------------------------------------------


def test_nested_pydantic_loc_maps_back_to_the_form_field():
    """Validated through the real gate, so the loc is the real one.

    `CwpMetrics(cls=-0.1)` on its own reports `('cls',)`; the application never
    validates that way, and a test that did would be pinning a path no request
    can reach.

    A *negative* CLS is the invalid value here because CLS has no upper bound —
    only the floor is a real constraint.
    """
    from ingest.manual import build_manual_run

    with pytest.raises(ValidationError) as exc:
        build_manual_run(**form.parse(submitted(cls="-0.1"))[0])
    errors = form.field_errors(exc.value)
    assert "cls" in errors
    assert "greater than or equal to 0" in errors["cls"]


def test_page_and_project_url_errors_do_not_collide():
    """Both report loc `(..., 'url')`; only the full path tells them apart."""
    names = {f.loc: f.name for f in form.FIELDS}
    assert names[("page", "url")] == "page_url"
    assert names[("project", "url")] == "project_url"


def test_an_unrecognised_loc_becomes_a_form_level_error():
    with pytest.raises(ValidationError) as exc:
        Run.model_validate({"run_id": "r"})
    assert form.FORM_LEVEL in form.field_errors(exc.value)


def test_manual_error_field_distinguishes_the_two_urls():
    assert form.manual_error_field("Invalid page url: bad scheme") == "page_url"
    assert form.manual_error_field("Invalid project url: bad scheme") == "project_url"


# --- grouping ---------------------------------------------------------------


def test_every_field_belongs_to_a_declared_group():
    assert {f.group for f in form.FIELDS} == set(form.GROUPS)
    assert sum(len(fields) for _, fields in form.grouped()) == len(form.FIELDS)
