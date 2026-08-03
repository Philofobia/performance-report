"""Unit tests for report/skeleton.py - the fixed-skeleton drift guard.

The guarantee this protects (PROJECT_SPEC §6.2) is that every report has the
same sections in the same order regardless of the data. The test that proves
it is cross-campaign: a one-page report and a three-page report must produce
the same fingerprint.
"""
from __future__ import annotations

from report.skeleton import PAGE_GROUP, collapse, fingerprint


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
