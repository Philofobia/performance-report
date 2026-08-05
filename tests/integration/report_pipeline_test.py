"""Integration tests for the rendering pipeline and its CLI.

No browser: every test runs with --no-pdf, matching the offline constraint.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from analysis.reportmodel import to_json
from report.__main__ import find_report, load_report, main
from report.skeleton import fingerprint
from tests.unit.render_html_test import a_report


@pytest.fixture
def campaign_dir(tmp_path):
    directory = tmp_path / "reports" / "storefront-abc12345"
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(
        to_json(a_report(("homepage", "plp"))), encoding="utf-8"
    )
    return directory


@pytest.fixture
def a_report_json(campaign_dir):
    """A freshly-written report.json, for tests that only need the file."""
    return campaign_dir / "report.json"


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def test_find_report_accepts_an_explicit_path(campaign_dir):
    target = campaign_dir / "report.json"
    assert find_report(input_path=target) == target


def test_find_report_resolves_a_campaign_id(campaign_dir):
    found = find_report(campaign="storefront-abc12345",
                        reports_dir=campaign_dir.parent)
    assert found == campaign_dir / "report.json"


def test_find_report_picks_the_newest_campaign_when_given_nothing(campaign_dir):
    older = campaign_dir.parent / "storefront-00000000"
    older.mkdir()
    (older / "report.json").write_text(
        to_json(a_report(("homepage",))), encoding="utf-8"
    )
    now = time.time()
    os.utime(older / "report.json", (now - 500, now - 500))
    os.utime(campaign_dir / "report.json", (now, now))

    assert find_report(reports_dir=campaign_dir.parent) == campaign_dir / "report.json"


def test_find_report_raises_when_there_is_nothing(tmp_path):
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        find_report(reports_dir=empty)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def test_load_report_validates_against_the_phase_4_schema(campaign_dir):
    report = load_report(campaign_dir / "report.json")
    assert report.cover.campaign_id == "storefront-abc12345"


def test_load_report_rejects_a_document_that_is_not_a_report(tmp_path):
    bad = tmp_path / "report.json"
    bad.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_report(bad)


def test_load_report_rejects_invalid_json(tmp_path):
    bad = tmp_path / "report.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_report(bad)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_writes_html_and_markdown(campaign_dir, capsys):
    assert main(["--input", str(campaign_dir / "report.json"), "--no-pdf"]) == 0
    assert (campaign_dir / "report.html").exists()
    assert (campaign_dir / "report.md").exists()
    assert not (campaign_dir / "report.pdf").exists()
    out = capsys.readouterr().out
    assert "report.html" in out


def test_cli_honours_an_explicit_output_dir(campaign_dir, tmp_path):
    out = tmp_path / "elsewhere"
    assert main(["--input", str(campaign_dir / "report.json"),
                 "--output-dir", str(out), "--no-pdf"]) == 0
    assert (out / "report.html").exists()


def test_cli_reports_a_missing_report_without_a_traceback(tmp_path, capsys):
    empty = tmp_path / "none"
    empty.mkdir()
    assert main(["--input", str(empty / "report.json"), "--no-pdf"]) != 0
    assert "not found" in capsys.readouterr().err.lower()


def test_cli_reports_an_invalid_report_without_a_traceback(tmp_path, capsys):
    bad = tmp_path / "report.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["--input", str(bad), "--no-pdf"]) != 0
    assert capsys.readouterr().err.strip()


# --------------------------------------------------------------------------- #
# purity and skeleton
# --------------------------------------------------------------------------- #
def test_rendering_the_same_report_twice_is_byte_identical(campaign_dir):
    source = campaign_dir / "report.json"
    main(["--input", str(source), "--no-pdf"])
    first = (campaign_dir / "report.html").read_text(encoding="utf-8")
    main(["--input", str(source), "--no-pdf"])
    second = (campaign_dir / "report.html").read_text(encoding="utf-8")
    assert first == second


def test_the_written_html_keeps_the_skeleton(campaign_dir, tmp_path):
    main(["--input", str(campaign_dir / "report.json"), "--no-pdf"])
    multi = fingerprint((campaign_dir / "report.html").read_text(encoding="utf-8"))

    single_dir = tmp_path / "single"
    single_dir.mkdir()
    (single_dir / "report.json").write_text(
        to_json(a_report(("homepage",))), encoding="utf-8"
    )
    main(["--input", str(single_dir / "report.json"), "--no-pdf"])
    single = fingerprint((single_dir / "report.html").read_text(encoding="utf-8"))

    assert multi == single


# --------------------------------------------------------------------------- #
# appendix images
# --------------------------------------------------------------------------- #
def test_no_appendix_images_leaves_no_embedded_image(tmp_path, a_report_json):
    out = tmp_path / "out"
    code = main([
        "--input", str(a_report_json), "--output-dir", str(out),
        "--no-pdf", "--no-appendix-images",
    ])
    assert code == 0
    assert "data:image/png;base64," not in (out / "report.html").read_text(encoding="utf-8")


def test_the_appendix_section_renders_even_with_images_disabled(tmp_path, a_report_json):
    out = tmp_path / "out"
    main(["--input", str(a_report_json), "--output-dir", str(out),
          "--no-pdf", "--no-appendix-images"])
    assert 'data-section="appendix"' in (out / "report.html").read_text(encoding="utf-8")


def test_a_report_whose_artifacts_were_deleted_still_renders(tmp_path, a_report_json):
    # data/raw gets cleaned; a months-old campaign must still re-render.
    out = tmp_path / "out"
    assert main(["--input", str(a_report_json), "--output-dir", str(out),
                "--no-pdf"]) == 0
