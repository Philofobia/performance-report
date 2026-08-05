"""Unit tests for report/render_md.py - the Markdown mirror."""
from __future__ import annotations

import re

from analysis.reportmodel import Report
from report.render_md import MD_SECTIONS, render_md
from tests.unit.render_html_test import a_report, an_appendix_entry


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


def test_the_mirror_carries_the_appendix_section():
    md = render_md(Report.model_validate(a_report(appendix=[an_appendix_entry()])))
    assert "## Appendix" in md


def test_the_screenshot_is_linked_relative_to_the_report(tmp_path):
    shot = tmp_path / "raw" / "homepage" / "screenshot.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"")
    out = tmp_path / "reports" / "campaign"
    out.mkdir(parents=True)

    report = Report.model_validate(
        a_report(appendix=[an_appendix_entry(screenshot=str(shot))])
    )
    md = render_md(report, base_dir=out)
    assert "../../raw/homepage/screenshot.png" in md.replace("\\", "/")


def test_no_base_dir_links_the_absolute_path():
    # Without a base_dir there is nothing to compute a relative path against,
    # so the screenshot is linked absolutely. This does not exercise the
    # os.path.relpath ValueError fallback — see the cross-drive test below,
    # which does, by monkeypatching relpath rather than by omitting base_dir.
    report = Report.model_validate(
        a_report(appendix=[an_appendix_entry(screenshot="/abs/shot.png")])
    )
    md = render_md(report, base_dir=None)
    assert "/abs/shot.png" in md.replace("\\", "/")


def test_a_cross_drive_path_falls_back_to_absolute(tmp_path, monkeypatch):
    # Windows: a report on C: and artifacts on D: have no relative path, and
    # os.path.relpath raises ValueError rather than returning something
    # usable. A real base_dir is passed so the `if base_dir is not None`
    # branch is taken and the try/except is the code path actually under
    # test — passing base_dir=None here would never reach os.path.relpath at
    # all, which is exactly the bug this replaces.
    import report.render_md as render_md_module

    def raise_value_error(*_args, **_kwargs):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(render_md_module.os.path, "relpath", raise_value_error)

    shot = tmp_path / "shot.png"
    report = Report.model_validate(
        a_report(appendix=[an_appendix_entry(screenshot=str(shot))])
    )
    md = render_md(report, base_dir=tmp_path / "reports")
    assert str(shot).replace("\\", "/") in md.replace("\\", "/")


def test_the_request_table_is_a_markdown_table():
    md = render_md(Report.model_validate(a_report(appendix=[an_appendix_entry()])))
    assert "| Request | Type | Status | Transfer | Time (ms) |" in md


def test_a_degraded_capture_states_its_reason_in_the_mirror():
    report = Report.model_validate(a_report(appendix=[
        an_appendix_entry(requests=False, degraded=["HAR malformed: line 1"])
    ]))
    assert "HAR malformed: line 1" in render_md(report)


def test_a_degraded_reason_surfaces_even_when_the_har_parsed_fine():
    # design §8's "path set, file gone" case: the HAR parses fine (requests
    # are present, total_requests > 0) but the screenshot file is missing.
    # The `{% if entry.total_requests %}` branch never sees `degraded` in
    # that case, and render_md has no images map to notice the screenshot
    # is gone the way render_html does — so `degraded` is the only signal
    # the mirror has, and must not stay silent about it.
    report = Report.model_validate(a_report(appendix=[
        an_appendix_entry(requests=True, degraded=["screenshot file missing"])
    ]))
    md = render_md(report)
    assert "screenshot file missing" in md
    # The requests summary line still renders normally alongside it.
    assert "Heaviest 1 of 214 requests" in md


def test_no_screenshot_retained_renders_its_empty_state():
    report = Report.model_validate(a_report(appendix=[
        an_appendix_entry(screenshot=None)
    ]))
    assert "No screenshot was retained for this capture." in render_md(report)


def test_an_empty_appendix_still_renders_the_heading():
    assert "## Appendix" in render_md(Report.model_validate(a_report(appendix=[])))


def test_the_har_size_is_stated_through_the_same_filter_as_the_html():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    md = render_md(report)
    entry = report.appendix[0]
    from report.render_html import transfer_size

    assert transfer_size(entry.har_bytes) in md


def test_unknown_and_zero_transfer_size_render_differently_in_the_markdown_table():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry(
        request_rows=[
            {"url": "https://example.com/cached.js", "resource_type": "script",
             "status": 304, "transfer_bytes": 0, "duration_ms": 5.0},
            {"url": "https://example.com/unmeasured.js", "resource_type": "script",
             "status": 200, "transfer_bytes": None, "duration_ms": 12.0},
        ],
        total_transfer_bytes=0,
    )]))
    md = render_md(report)
    cached_row = next(line for line in md.splitlines() if "cached.js" in line)
    unmeasured_row = next(line for line in md.splitlines() if "unmeasured.js" in line)
    assert "| 0 B |" in cached_row
    assert "| — |" in unmeasured_row
    assert "0 B" not in unmeasured_row
    assert "| — |" not in cached_row


def test_an_unknown_total_reads_sensibly_in_the_markdown_mirror():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry(
        total_transfer_bytes=None,
    )]))
    md = render_md(report)
    assert "total transferred size unknown" in md
    assert "— transferred in total" not in md


def test_a_pipe_in_a_url_does_not_shift_the_table_columns():
    report = Report.model_validate(a_report(appendix=[an_appendix_entry()]))
    entry = report.appendix[0]
    entry.requests[0].url = "https://example.com/hero.mp4?a=1|b=2"
    md = render_md(report)
    row = next(
        line for line in md.splitlines() if "hero.mp4" in line and line.startswith("|")
    )
    assert "a=1\\|b=2" in row
    # A GFM parser stops at an unescaped `|`; splitting on one that isn't
    # preceded by a backslash is exactly what it does. 5 columns delimited by
    # leading/trailing pipes split into 7 parts (2 empty boundaries + 5 cells)
    # -- the escaped pipe inside the URL must not add an 8th.
    cells = re.split(r"(?<!\\)\|", row)
    assert len(cells) == 7
