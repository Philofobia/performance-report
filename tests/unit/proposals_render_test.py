"""The "New tickets" section and proposed-tickets.md (analysis/discovery)."""
from __future__ import annotations

from analysis.reportmodel import ProposalModel
from report.__main__ import write_outputs
from report.render_html import render_html
from report.render_md import render_md, render_proposed_tickets
from report.skeleton import BASELINE_PATH, fingerprint, load_baseline
from tests.unit.render_html_test import a_report


def _proposals():
    return [
        ProposalModel(
            key="short_static_cache", title="All pages | 128 static assets are cached for "
            "under a week (102 with max-age 1 hour)", category="Caching", severity="high",
            summary="128 of the site's own assets are cached for under a week.",
            fix="Serve versioned assets with max-age=31536000, immutable.",
            done_when="Every versioned asset carries a max-age of a year.",
            seen_on=["homepage/mobile", "pdp/mobile"], captures_total=2,
            evidence=["homepage / mobile (HOMEMOB.har, 3 runs): 100 static assets",
                      "  max-age 1 hour on 69 asset(s), e.g. <script>alert(1)</script>"],
            related=["OAK-39154"]),
        ProposalModel(
            key="dom_size", title="All pages | The page builds 9,056 DOM elements",
            category="Rendering", severity="high", summary="s", fix="f", done_when="d",
            seen_on=["homepage/mobile"], captures_total=2, tracked_by="OAK-1"),
    ]


def test_the_section_lists_drafts_with_fix_and_done_when_and_escapes_evidence():
    report = a_report()
    report.proposals = _proposals()
    html = render_html(report)

    assert "128 static assets are cached for under a week" in html
    assert "Serve versioned assets with max-age=31536000, immutable." in html
    assert "Every versioned asset carries a max-age of a year." in html
    assert "OAK-39154" in html
    assert "<script>alert(1)</script>" not in html
    # A tracked rule is listed as already filed, not proposed again.
    assert "Already filed" in html and "OAK-1" in html


def test_proposals_do_not_change_the_skeleton():
    report = a_report(pages=("homepage", "pdp"))
    report.proposals = _proposals()
    assert fingerprint(render_html(report)) == load_baseline(BASELINE_PATH)


def test_without_hars_the_section_says_why_it_is_empty():
    assert "nothing was looked for" in render_html(a_report())
    assert "nothing was looked for" in render_md(a_report())


def test_the_drafts_file_holds_one_block_per_untracked_proposal():
    report = a_report()
    report.proposals = _proposals()
    drafts = render_proposed_tickets(report)

    assert drafts.count("\n## All pages | ") == 1
    assert "**Priority:** High · **Area:** Caching" in drafts
    assert "### Proposed fix" in drafts and "### Done when" in drafts
    assert "- **OAK-1** tracks: All pages | The page builds 9,056 DOM elements" in drafts


def test_the_drafts_file_is_written_and_removed_with_the_proposals(tmp_path):
    report = a_report()
    report.proposals = _proposals()
    write_outputs(report, output_dir=tmp_path, with_pdf=False)
    assert (tmp_path / "proposed-tickets.md").exists()

    report.proposals = []
    write_outputs(report, output_dir=tmp_path, with_pdf=False)
    assert not (tmp_path / "proposed-tickets.md").exists()
