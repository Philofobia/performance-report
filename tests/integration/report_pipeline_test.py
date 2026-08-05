"""Integration tests for the rendering pipeline and its CLI.

No browser: every test runs with --no-pdf, matching the offline constraint.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from PIL import Image

import config.load as config_load
from analysis.reportmodel import to_json
from config.load import Settings, StorageConfig
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
@pytest.fixture
def appendix_capture(tmp_path):
    """A report.json whose sole appendix entry points at a real screenshot.

    ``raw_dir`` is a real directory holding a genuine PNG (not a stub path),
    and ``settings`` is a real :class:`Settings` with only ``storage.raw_dir``
    overridden to point at it — the model that ``build_appendix_images``
    actually confines paths against, not a hand-rolled substitute.
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shot = raw_dir / "shot.png"
    Image.new("RGB", (12, 8), color=(200, 40, 40)).save(shot)

    campaign = tmp_path / "reports" / "storefront-abc12345"
    campaign.mkdir(parents=True)
    report_json = campaign / "report.json"
    report_json.write_text(to_json(a_report(
        ("homepage",),
        appendix=[{
            "page": "homepage", "run_id": "run_homepage", "device": "mid-mobile",
            "network": "slow-4g", "screenshot": str(shot),
            "har": None, "har_sha256": None, "har_bytes": None,
            "requests": [], "total_requests": 0, "total_transfer_bytes": 0,
            "degraded": [],
        }],
    )), encoding="utf-8")

    settings = Settings(storage=StorageConfig(raw_dir=str(raw_dir)))
    return report_json, settings


def test_appendix_images_are_embedded_when_a_capture_exists(
    tmp_path, appendix_capture, monkeypatch
):
    # The positive case: proves build_appendix_images is actually wired into
    # main(), not just that the flag is accepted.
    report_json, settings = appendix_capture
    monkeypatch.setattr(config_load, "load_settings", lambda: settings)

    out = tmp_path / "out"
    code = main(["--input", str(report_json), "--output-dir", str(out), "--no-pdf"])
    assert code == 0
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in html
    assert 'data-section="appendix"' in html


def test_no_appendix_images_omits_the_embedded_image(
    tmp_path, appendix_capture, monkeypatch
):
    # Same real capture as the positive test above; only the flag differs.
    # Without a passing positive test, this one is meaningless — a renderer
    # that never embeds anything would also satisfy it.
    report_json, settings = appendix_capture
    monkeypatch.setattr(config_load, "load_settings", lambda: settings)

    out = tmp_path / "out"
    code = main(["--input", str(report_json), "--output-dir", str(out),
                 "--no-pdf", "--no-appendix-images"])
    assert code == 0
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," not in html
    assert 'data-section="appendix"' in html


def test_a_missing_screenshot_degrades_to_path_only_rows(tmp_path, a_report_json):
    # data/raw gets cleaned; a months-old campaign must still re-render.
    # `a_report_json`'s appendix entry points at "shot.png", which exists
    # nowhere — this exercises the missing-capture path, not the flag.
    out = tmp_path / "out"
    code = main(["--input", str(a_report_json), "--output-dir", str(out), "--no-pdf"])
    assert code == 0
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," not in html
    assert 'data-section="appendix"' in html


def test_an_undecodable_settings_file_degrades_to_path_only_rows_with_a_stderr_note(
    tmp_path, a_report_json, monkeypatch, capsys
):
    # A settings.yaml saved in a non-UTF-8 encoding is plausible on Windows.
    # `load_yaml` only wraps `yaml.YAMLError`, not the open/read itself, so
    # this must be handled at the call site in report/__main__.py.
    bad_settings = tmp_path / "settings.yaml"
    bad_settings.write_bytes(b"\xff\xfe\x00\x01not valid utf-8")
    real_load_settings = config_load.load_settings
    monkeypatch.setattr(
        config_load, "load_settings", lambda: real_load_settings(bad_settings)
    )

    out = tmp_path / "out"
    code = main(["--input", str(a_report_json), "--output-dir", str(out), "--no-pdf"])
    assert code == 0
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," not in html
    assert 'data-section="appendix"' in html
    assert "Appendix images skipped" in capsys.readouterr().err
