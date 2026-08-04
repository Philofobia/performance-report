"""Unit tests for report/skeleton.py - the fixed-skeleton drift guard.

The guarantee this protects (PROJECT_SPEC §6.2) is that every report has the
same sections in the same order regardless of the data. The test that proves
it is cross-campaign: a one-page report and a three-page report must produce
the same fingerprint.

Phase 6 adds the committed baseline. The test that matters there is
``test_the_committed_baseline_matches_the_real_template``: without it a
template change plus a forgotten ``--update-baseline`` leaves a stale baseline
sitting in the repo, and nobody notices until someone happens to run
``--skeleton-check`` against a real campaign.
"""
from __future__ import annotations

import json

import pytest

from report.skeleton import (
    BASELINE_PATH,
    BASELINE_VERSION,
    PAGE_GROUP,
    collapse,
    diff_sections,
    fingerprint,
    format_drift,
    load_baseline,
    save_baseline,
)


def page_block(name):
    return (
        f'<section data-section="page" data-page="{name}">'
        '<div data-section="page.header"></div>'
        '<div data-section="page.cwv-dashboard"></div>'
        '<div data-section="page.findings"></div>'
        "</section>"
    )


def document(pages):
    body = "".join(page_block(p) for p in pages)
    return (
        '<section data-section="cover"></section>'
        '<section data-section="summary"></section>'
        f"{body}"
        '<section data-section="comparison"></section>'
        '<section data-section="methodology"></section>'
    )


def test_fingerprint_returns_sections_in_document_order():
    assert fingerprint(document(["homepage"])) == [
        "cover", "summary", PAGE_GROUP, "page.header", "page.cwv-dashboard",
        "page.findings", "comparison", "methodology",
    ]


def test_elements_without_a_data_section_are_ignored():
    html = '<div class="wrapper"><section data-section="cover"></section></div>'
    assert fingerprint(html) == ["cover"]


def test_a_one_page_and_a_three_page_report_share_a_fingerprint():
    one = fingerprint(document(["homepage"]))
    three = fingerprint(document(["homepage", "pdp", "plp"]))
    assert one == three


def test_a_report_with_no_pages_still_has_the_outer_sections():
    assert fingerprint(document([])) == [
        "cover", "summary", "comparison", "methodology",
    ]


def test_removing_a_section_changes_the_fingerprint():
    intact = fingerprint(document(["homepage"]))
    broken = fingerprint(document(["homepage"]).replace(
        '<div data-section="page.findings"></div>', ""
    ))
    assert intact != broken
    assert "page.findings" not in broken


def test_reordering_sections_changes_the_fingerprint():
    intact = document(["homepage"])
    swapped = (
        '<section data-section="summary"></section>'
        '<section data-section="cover"></section>'
    ) + intact.split('<section data-section="summary"></section>')[1]
    assert fingerprint(intact) != fingerprint(swapped)


def test_collapse_folds_repeated_page_blocks_into_one_group():
    raw = ["cover", "page", "page.header", "page", "page.header", "comparison"]
    assert collapse(raw) == ["cover", PAGE_GROUP, "page.header", "comparison"]


def test_collapse_leaves_a_document_without_pages_untouched():
    assert collapse(["cover", "summary"]) == ["cover", "summary"]


def test_collapse_keeps_sections_that_merely_start_with_the_word_page():
    # "pagination" is not part of the repeating page block.
    assert collapse(["pagination", "cover"]) == ["pagination", "cover"]


# --- Phase 6: the committed baseline ---------------------------------------


def test_baseline_round_trips(tmp_path):
    path = tmp_path / "baseline.json"
    save_baseline(["cover", PAGE_GROUP, "page.header"], path)
    assert load_baseline(path) == ["cover", PAGE_GROUP, "page.header"]


def test_saved_baseline_is_diff_friendly(tmp_path):
    path = tmp_path / "baseline.json"
    save_baseline(["cover", "summary"], path)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    # One section per line, so a drift commit reads as a one-line diff.
    assert '\n        "cover",\n' in text


def test_loading_a_missing_baseline_names_the_path(tmp_path):
    with pytest.raises(ValueError, match="baseline.json"):
        load_baseline(tmp_path / "baseline.json")


def test_a_malformed_baseline_is_rejected(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="baseline.json"):
        load_baseline(path)


def test_a_baseline_from_another_algorithm_version_is_rejected(tmp_path):
    # A change to how fingerprints are computed must not read as mass drift.
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps({"version": BASELINE_VERSION + 1, "sections": ["cover"]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="version"):
        load_baseline(path)


def test_identical_section_lists_produce_no_diff():
    assert diff_sections(["cover", "summary"], ["cover", "summary"]) == []


def test_a_removed_section_is_reported_as_a_removal():
    assert diff_sections(["cover", "summary"], ["cover"]) == [("-", "summary", 1)]


def test_an_added_section_is_reported_as_an_addition():
    assert diff_sections(["cover"], ["cover", "summary"]) == [("+", "summary", 1)]


def test_a_reordered_section_reads_as_a_removal_plus_an_addition():
    # Not as a wholesale mismatch from the first differing index onward: one
    # of the two swapped sections moves, the other and every untouched
    # section stay out of the diff entirely.
    diff = diff_sections(["cover", "summary", "methodology"],
                         ["cover", "methodology", "summary"])
    assert [sign for sign, _, _ in sorted(diff)] == ["+", "-"]
    moved = {section for _, section, _ in diff}
    assert len(moved) == 1
    assert moved < {"summary", "methodology"}
    assert "cover" not in moved


def test_format_drift_names_the_baseline_and_every_change():
    text = format_drift([("-", "page.findings", 4), ("+", "page.waterfall", 4)],
                        path="report/skeleton.baseline.json")
    assert "report/skeleton.baseline.json" in text
    assert "- page.findings" in text
    assert "+ page.waterfall" in text


def test_the_committed_baseline_matches_the_real_template():
    """The guard that needs no campaign.

    A template change plus a forgotten ``--update-baseline`` is caught here,
    offline, instead of the next time somebody renders a real report.
    """
    from report.render_html import render_html

    from tests.unit.render_html_test import a_report

    assert fingerprint(render_html(a_report())) == load_baseline(BASELINE_PATH)


def test_the_committed_baseline_is_page_count_independent():
    from report.render_html import render_html

    from tests.unit.render_html_test import a_report

    three = fingerprint(render_html(a_report(pages=("homepage", "pdp", "plp"))))
    assert three == load_baseline(BASELINE_PATH)
