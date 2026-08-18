"""Integration tests for the unified CLI.

Drives real runs through `python -m cli`'s dispatch — analyze, then report,
then the skeleton drift guard — with no browser, no network and no API key.
The unit tests prove the façade forwards argv; these prove the argv it
forwards actually produces a report.
"""
from __future__ import annotations

import json

import pytest

import cli
from report.skeleton import BASELINE_PATH, load_baseline, save_baseline
from store import sql
from tests.integration.analysis_pipeline_test import run_payload


@pytest.fixture()
def input_dir(tmp_path):
    directory = tmp_path / "processed"
    directory.mkdir()
    for run_id, page in (("run_h1", "homepage"), ("run_p1", "plp")):
        (directory / f"{run_id}.json").write_text(
            json.dumps(run_payload(run_id, page)), encoding="utf-8"
        )
    return directory


@pytest.fixture()
def campaign(input_dir, tmp_path):
    """A rendered-ready campaign, produced through the façade."""
    reports = tmp_path / "reports"
    assert cli.main(["analyze", "--input-dir", str(input_dir),
                     "--output-dir", str(reports), "--no-llm"]) == 0
    return next(reports.glob("*/report.json"))


# --- the pipeline through one entry point -----------------------------------


def test_the_facade_exposes_the_manual_entry_ui():
    """`python -m cli ui` reaches webui, and the table advertises it."""
    assert "ui" in cli.COMMANDS
    assert cli._DELEGATES["ui"]() is not None
    assert "ui" in cli.usage()


def test_analyze_then_report_produces_the_deliverables(campaign):
    assert cli.main(["report", "--input", str(campaign), "--no-pdf"]) == 0
    assert (campaign.parent / "report.html").exists()
    assert (campaign.parent / "report.md").exists()


def test_analyze_flags_survive_the_facade(input_dir, tmp_path):
    reports = tmp_path / "reports"
    assert cli.main(["analyze", "--input-dir", str(input_dir),
                     "--output-dir", str(reports), "--pages", "plp",
                     "--no-llm"]) == 0
    payload = json.loads(next(reports.glob("*/report.json")).read_text(encoding="utf-8"))
    assert payload["cover"]["pages"] == ["plp"]


def test_a_stage_failure_surfaces_as_a_non_zero_exit(tmp_path, capsys):
    empty = tmp_path / "none"
    empty.mkdir()
    assert cli.main(["analyze", "--input-dir", str(empty), "--no-llm"]) != 0
    assert capsys.readouterr().err.strip()


# --- list-runs --------------------------------------------------------------


def test_list_runs_shows_what_the_store_holds(tmp_path, capsys):
    from normalize.schema import Run

    db = tmp_path / "runs.sqlite"
    conn = sql.connect(db)
    sql.insert_run(conn, Run.model_validate(run_payload("run_h1", "homepage")))
    conn.close()

    assert cli.main(["list-runs", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "run_h1" in out
    assert "homepage" in out


# --- the drift guard --------------------------------------------------------


def test_skeleton_check_passes_against_the_committed_baseline(campaign, capsys):
    assert cli.main(["report", "--input", str(campaign), "--no-pdf",
                     "--skeleton-check"]) == 0
    assert "skeleton ok" in capsys.readouterr().out


def test_a_drifted_baseline_fails_but_still_writes_the_report(
    campaign, tmp_path, capsys
):
    # The rendered output is the evidence for diagnosing the drift, so the
    # command that detects it must not withhold it.
    drifted = tmp_path / "drifted.json"
    save_baseline(load_baseline(BASELINE_PATH) + ["page.waterfall"], drifted)

    assert cli.main(["report", "--input", str(campaign), "--no-pdf",
                     "--skeleton-check", "--baseline", str(drifted)]) == 1
    assert (campaign.parent / "report.html").exists()
    err = capsys.readouterr().err
    assert "skeleton drift" in err
    assert "page.waterfall" in err


def test_update_baseline_writes_the_current_structure(campaign, tmp_path, capsys):
    target = tmp_path / "fresh.json"
    assert cli.main(["report", "--input", str(campaign), "--no-pdf",
                     "--update-baseline", "--baseline", str(target)]) == 0
    assert load_baseline(target) == load_baseline(BASELINE_PATH)
    assert "baseline updated" in capsys.readouterr().out


def test_check_and_update_cannot_be_combined(campaign):
    with pytest.raises(SystemExit):
        cli.main(["report", "--input", str(campaign), "--no-pdf",
                  "--skeleton-check", "--update-baseline"])
