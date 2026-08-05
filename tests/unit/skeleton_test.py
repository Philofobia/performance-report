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
    GROUP_ROOTS,
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


# --- Phase 7B: a second repeating root (capture) ----------------------------
#
# The real appendix markup wraps a non-repeating section, tagged
# data-section="appendix", around N repeating per-capture entries. Using
# "appendix" for both the wrapper and the repeating entry was tried first and
# is wrong: collapse() opens a block on the wrapper's bare "appendix", the
# very next token is the first entry's bare "appendix" (not
# "appendix."-prefixed), so the wrapper's block closes with zero children and
# the entry block that actually carries the children is discarded as already
# emitted. The fingerprint silently loses capture.screenshot/capture.requests
# for every campaign, which is exactly the kind of drift this module exists
# to catch. The fix names the repeating entry "capture" instead, so it cannot
# collide with the wrapper. See test_the_old_appendix_reuse_would_lose_the_
# capture_children below for the locked-in regression.


def capture_entry():
    return (
        '<article data-section="capture">'
        '<figure data-section="capture.screenshot"></figure>'
        '<table data-section="capture.requests"></table>'
        "</article>"
    )


def appendix_block(count):
    return (
        '<section data-section="appendix">'
        + "".join(capture_entry() for _ in range(count))
        + "</section>"
    )


def test_repeated_capture_entries_collapse_to_one_group():
    sections = ["capture", "capture.screenshot", "capture.requests"] * 4
    assert collapse(sections) == [
        "capture[]", "capture.screenshot", "capture.requests",
    ]


def test_a_one_capture_and_a_six_capture_report_fingerprint_identically():
    # The same argument as one page versus three pages: the skeleton must be
    # independent of how much data the campaign happened to produce. Uses the
    # real nested shape: an "appendix" wrapper section around N "capture"
    # entry articles.
    def document_with(count):
        return document(["homepage"]) + appendix_block(count)

    one = fingerprint(document_with(1))
    six = fingerprint(document_with(6))
    assert one == six
    # Equality alone isn't enough: both sides degrading to the same *empty*
    # group would satisfy `one == six` too. Assert the children actually
    # survived the collapse.
    assert one[-4:] == [
        "appendix", "capture[]", "capture.screenshot", "capture.requests",
    ]


def test_appendix_wrapper_survives_as_its_own_ungrouped_section():
    # The wrapper is genuinely non-repeating, like "methodology" — it must
    # stay a plain section, distinct from the "capture[]" group it contains.
    sections = [
        "cover",
        "page", "page.header", "page", "page.header",
        "methodology",
        "appendix",
        "capture", "capture.screenshot", "capture", "capture.screenshot",
    ]
    assert collapse(sections) == [
        "cover", "page[]", "page.header", "methodology",
        "appendix", "capture[]", "capture.screenshot",
    ]


def test_a_document_without_an_appendix_fingerprints_exactly_as_before():
    # Proves the generalization is not itself a drift event.
    sections = ["cover", "page", "page.header", "page", "page.header", "methodology"]
    assert collapse(sections) == ["cover", "page[]", "page.header", "methodology"]


def test_a_group_root_with_no_children_is_still_emitted():
    assert collapse(["cover", "capture", "capture"]) == ["cover", "capture[]"]


def test_the_old_appendix_reuse_would_lose_the_capture_children():
    # Regression lock for the rejected design: GROUP_ROOTS = ("page",
    # "appendix") with the wrapper and the repeating entry both tagged
    # "appendix" silently drops the children from the fingerprint, because
    # the wrapper's own block closes before any child is seen and the entry
    # block that carries the children is then discarded as a duplicate.
    sections = [
        "methodology",
        "appendix",  # wrapper
        "appendix", "appendix.screenshot", "appendix.requests",  # entry
    ]
    broken = collapse(sections, roots=("page", "appendix"))
    assert broken == ["methodology", "appendix[]"]
    assert "appendix.screenshot" not in broken
    assert "appendix.requests" not in broken
