"""Unit tests for report/glossary.py — the committed plain-language layer."""
from __future__ import annotations

import pytest

from config.load import Thresholds
from report.glossary import GlossaryError, load_glossary


@pytest.fixture(scope="module")
def gl():
    return load_glossary()


def test_every_metric_the_report_renders_has_an_entry(gl):
    """The at-a-glance table draws from these; a gap renders a bare number."""
    for metric in ("lcp_ms", "cls", "inp_ms", "fcp_ms", "ttfb_ms", "tbt_ms"):
        assert gl.has(metric), metric
        assert gl.gloss(metric)
        assert gl.label(metric)


def test_the_gloss_avoids_the_jargon_it_exists_to_replace(gl):
    """A gloss that says "largest contentful paint" has explained nothing."""
    assert "contentful" not in gl.gloss("lcp_ms").lower()
    assert "blocking time" not in gl.gloss("tbt_ms").lower()


def test_milliseconds_render_as_whole_numbers(gl):
    assert gl.format_value("lcp_ms", 2438.5999999940395) == "2439 ms"


def test_cls_renders_to_two_decimals(gl):
    assert gl.format_value("cls", 0.015911182251991944) == "0.02"


def test_an_unglossed_metric_falls_back_rather_than_raising(gl):
    assert gl.has("made_up_metric") is False
    assert gl.format_value("made_up_metric", 12.5) == "12.5"
    assert gl.label("made_up_metric") == "made_up_metric"
    assert gl.gloss("made_up_metric") == ""


def test_a_missing_value_renders_as_a_dash(gl):
    assert gl.format_value("lcp_ms", None) == "—"


def test_targets_come_from_the_configured_thresholds(gl):
    thresholds = Thresholds()
    assert gl.target_for("lcp_ms", thresholds) == float(thresholds.lcp_good_ms)
    assert gl.target_for("cls", thresholds) == float(thresholds.cls_good)


def test_a_metric_with_no_configured_target_has_none(gl):
    assert gl.target_for("tbt_ms", Thresholds()) is None


def test_context_states_how_far_over_target_a_value_is(gl):
    assert gl.context("lcp_ms", 5000.0, 2500.0) == "2.0× over"
    assert gl.context("lcp_ms", 2000.0, 2500.0) == "within target"
    assert gl.context("lcp_ms", 2600.0, 2500.0) == "1.0× over"


def test_context_is_empty_without_a_target(gl):
    """An unconfigured threshold must not be reported as a pass."""
    assert gl.context("tbt_ms", 2041.0, None) == ""


def test_context_handles_a_zero_target_without_dividing_by_zero(gl):
    assert gl.context("cls", 0.4, 0.0) == "over target"


def test_a_malformed_glossary_is_a_clean_error(tmp_path):
    path = tmp_path / "glossary.yaml"
    path.write_text("lcp_ms: not-a-mapping\n", encoding="utf-8")

    with pytest.raises(GlossaryError, match="mapping"):
        load_glossary(path)


def test_a_missing_glossary_names_the_file(tmp_path):
    with pytest.raises(GlossaryError, match="glossary"):
        load_glossary(tmp_path / "absent.yaml")
