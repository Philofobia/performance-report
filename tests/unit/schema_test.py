"""Unit tests for normalize/schema.py (canonical Run object)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from normalize.schema import Run


def valid_run_payload(**overrides):
    payload = {
        "run_id": "run_20260108_1430_ab12",
        "project": {"name": "storefront", "url": "https://example.com"},
        "page": {"name": "homepage", "url": "https://example.com/"},
        "condition": {
            "device": "mid-mobile",
            "network": "slow-4g",
            "cpu_throttle": 4,
            "runs": 3,
        },
        "meta": {
            "created_at": "2026-01-08T14:30:00Z",
            "source": "automated",
            "runner": "cli-1.0",
        },
        "metrics": {
            "cwp": {
                "lcp_ms": 6200,
                "cls": 0.42,
                "inp_ms": 480,
                "fcp_ms": 3100,
                "ttfb_ms": 1800,
                "target_lcp_ms": 2500,
                "target_cls": 0.1,
                "target_inp_ms": 200,
            },
            "lighthouse": {"performance": 54, "accessibility": 88, "best_practices": 79, "seo": 90},
            "network": {"total_transfer_kb": 4820, "request_count": 118, "render_blocking_css": 6},
        },
    }
    return {**payload, **overrides}


def test_valid_automated_run_parses():
    run = Run.model_validate(valid_run_payload())
    assert run.run_id.startswith("run_")
    assert run.condition.device == "mid-mobile"
    assert run.metrics.cwp.lcp_ms == 6200
    assert run.metrics.lighthouse.performance == 54


def test_valid_manual_run_needs_no_cwv():
    payload = valid_run_payload(
        meta={
            "created_at": "2026-01-08T14:30:00Z",
            "source": "manual",
            "runner": "cli-1.0",
        },
        metrics={},  # manual runs may omit all metrics
    )
    run = Run.model_validate(payload)
    assert run.meta.source == "manual"


def test_automated_missing_cwv_trio_raises():
    payload = valid_run_payload()
    payload["metrics"]["cwp"] = {"lcp_ms": 1000}  # missing cls + inp
    with pytest.raises(ValidationError, match="CWV trio"):
        Run.model_validate(payload)


def test_negative_lcp_rejected():
    payload = valid_run_payload()
    payload["metrics"]["cwp"]["lcp_ms"] = -5
    with pytest.raises(ValidationError):
        Run.model_validate(payload)


def test_cls_out_of_range_rejected():
    payload = valid_run_payload()
    payload["metrics"]["cwp"]["cls"] = 1.5
    with pytest.raises(ValidationError):
        Run.model_validate(payload)


def test_cls_negative_rejected():
    payload = valid_run_payload()
    payload["metrics"]["cwp"]["cls"] = -0.1
    with pytest.raises(ValidationError):
        Run.model_validate(payload)


def test_non_numeric_metric_rejected():
    payload = valid_run_payload()
    payload["metrics"]["cwp"]["inp_ms"] = "abc"
    with pytest.raises(ValidationError):
        Run.model_validate(payload)


def test_lighthouse_score_out_of_range_rejected():
    payload = valid_run_payload()
    payload["metrics"]["lighthouse"]["performance"] = 150
    with pytest.raises(ValidationError):
        Run.model_validate(payload)


def test_lighthouse_negative_rejected():
    payload = valid_run_payload()
    payload["metrics"]["lighthouse"]["seo"] = -1
    with pytest.raises(ValidationError):
        Run.model_validate(payload)


def test_lighthouse_within_range_accepted():
    payload = valid_run_payload()
    payload["metrics"]["lighthouse"] = {"performance": 0, "seo": 100}
    run = Run.model_validate(payload)
    assert run.metrics.lighthouse.performance == 0
    assert run.metrics.lighthouse.seo == 100


def test_empty_run_id_rejected():
    with pytest.raises(ValidationError):
        Run.model_validate(valid_run_payload(run_id=""))


def test_missing_page_url_rejected():
    payload = valid_run_payload()
    del payload["page"]["url"]
    with pytest.raises(ValidationError):
        Run.model_validate(payload)
