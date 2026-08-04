"""Unit tests for report/render_md.py - the Markdown mirror."""
from __future__ import annotations

import re

from report.render_md import MD_SECTIONS, render_md
from tests.unit.render_html_test import a_report


def headings(markdown: str):
    return re.findall(r"^## (.+)$", markdown, flags=re.MULTILINE)


def test_section_sequence_matches_the_declared_skeleton():
    assert tuple(headings(render_md(a_report()))) == MD_SECTIONS


def test_the_section_sequence_is_the_same_for_one_and_three_pages():
    assert headings(render_md(a_report(("homepage",)))) == headings(
        render_md(a_report(("homepage", "pdp", "plp")))
    )


def test_every_page_gets_its_own_subsection():
    markdown = render_md(a_report(("homepage", "plp")))
    assert "### homepage" in markdown
    assert "### plp" in markdown


def test_metrics_render_as_a_table():
    markdown = render_md(a_report())
    assert "| Condition |" in markdown
    assert "| --- |" in markdown


def test_comparison_renders_as_a_table():
    assert "| Page | Device |" in render_md(a_report())


def test_the_trend_mirrors_the_html_section():
    from tests.unit.render_html_test import a_trend

    markdown = render_md(a_report(trends=[a_trend()]))
    assert "**Trend**" in markdown
    assert "| Condition | Metric | History | Direction | Change |" in markdown
    assert "6200.0 → 4820.0" in markdown
    assert "-22.3%" in markdown
    # The mirror names metrics the way the HTML does, not by raw field name.
    # Scoped to the trend row: `lcp_ms=6200` legitimately appears elsewhere as
    # a finding's evidence.
    row = next(line for line in markdown.splitlines() if "6200.0 → 4820.0" in line)
    assert "| LCP |" in row
    assert "lcp_ms" not in row


def test_a_page_with_no_history_says_so_in_the_mirror():
    assert "No prior campaigns" in render_md(a_report())


def test_empty_recommendations_render_an_empty_state():
    markdown = render_md(a_report(recommendations=False))
    assert "No playbook-grounded recommendations" in markdown


def test_a_rule_based_report_says_so():
    assert "rule-based" in render_md(a_report(mode="rule_based")).lower()


def test_markdown_is_not_html_escaped():
    # Markdown is not HTML; escaping here would corrupt legitimate prose.
    # The module docstring documents that conversion to HTML is the
    # consumer's escaping boundary.
    markdown = render_md(a_report(finding_title="Cost < 2s & rising"))
    assert "Cost < 2s & rising" in markdown
    assert "&lt;" not in markdown


def test_rendering_is_a_pure_function():
    report = a_report()
    assert render_md(report) == render_md(report)
