"""Unit tests for ingest/manual.py — CLI + validation -> canonical Run."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ingest import manual
from ingest.manual import ManualValidationError, build_manual_run, main


def test_text_only_produces_manual_run():
    run = build_manual_run("Homepage LCP spikes to 6s", page_url="https://example.com/")
    assert run.meta.source == "manual"
    assert run.problem.description == "Homepage LCP spikes to 6s"
    assert run.metrics.cwp.lcp_ms is None  # no metrics supplied
    assert run.metrics.lighthouse.performance is None


def test_metrics_only_populates_cwp_and_validates():
    run = build_manual_run(
        page_url="https://example.com/",
        cwp={"lcp_ms": 6200, "cls": 0.42, "inp_ms": 480, "fcp_ms": 3100, "ttfb_ms": 1800},
        lighthouse={"performance": 54, "accessibility": 88},
        network_metrics={"total_transfer_kb": 4820, "request_count": 118},
    )
    assert run.metrics.cwp.lcp_ms == 6200
    assert run.metrics.cwp.cls == 0.42
    assert run.metrics.lighthouse.performance == 54
    assert run.metrics.network.request_count == 118
    assert run.problem.description == ""


def test_combined_text_and_metrics():
    run = build_manual_run(
        "Hero video bloats bundle",
        keywords=["bundle", "hero-video"],
        page_url="https://example.com/",
        cwp={"lcp_ms": 6200, "cls": 0.42, "inp_ms": 480},
        network_metrics={"total_transfer_kb": 2140},
    )
    assert run.problem.keywords == ["bundle", "hero-video"]
    assert run.metrics.network.total_transfer_kb == 2140


def test_run_id_and_required_fields_filled():
    run = build_manual_run(page_url="https://example.com/")
    assert run.run_id.startswith("run_")
    assert run.page.name and run.page.url
    assert run.condition.device == "mid-mobile"


def test_out_of_range_cls_rejected():
    with pytest.raises(ValidationError):
        build_manual_run(page_url="https://example.com/", cwp={"cls": 1.5})


def test_negative_lcp_rejected():
    with pytest.raises(ValidationError):
        build_manual_run(page_url="https://example.com/", cwp={"lcp_ms": -5})


def test_lighthouse_out_of_range_rejected():
    with pytest.raises(ValidationError):
        build_manual_run(page_url="https://example.com/", lighthouse={"performance": 150})


def test_non_https_url_rejected():
    with pytest.raises(ManualValidationError, match="[Ii]nvalid"):
        build_manual_run("problem", page_url="http://example.com/")


def test_raw_ip_url_rejected():
    with pytest.raises(ManualValidationError, match="[Ii]nvalid"):
        build_manual_run(page_url="https://192.168.0.1/")


def test_missing_page_url_rejected():
    with pytest.raises(ManualValidationError, match="page url"):
        build_manual_run("problem")


def test_invalid_source_rejected():
    with pytest.raises(ManualValidationError, match="source"):
        build_manual_run(page_url="https://example.com/", source="bogus")


def test_derive_keywords_from_problem_text():
    assert manual.derive_keywords("LCP is bad, CLS too, plus a big video") == ["lcp", "cls", "video"]
    assert manual.derive_keywords(None) == []
    assert manual.derive_keywords("") == []


def test_cli_success_writes_output(tmp_path, capsys):
    out = tmp_path / "runs" / "run.json"
    code = main([
        "--problem", "Slow LCP", "--lcp-ms", "6200",
        "--page", "homepage", "--page-url", "https://example.com/",
        "--output", str(out),
    ])
    assert code == 0
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["metrics"]["cwp"]["lcp_ms"] == 6200.0
    assert data["page"]["name"] == "homepage"


def test_cli_invalid_metric_returns_nonzero(capsys):
    code = main([
        "--cls", "1.9",  # out of 0..1
        "--page-url", "https://example.com/",
    ])
    assert code == 1
    err = capsys.readouterr().err
    assert "error" in err.lower()


def test_cli_missing_page_url_returns_nonzero(capsys):
    code = main(["--problem", "x" * 10])
    assert code == 1
