"""Field rules are rule-based, so the no-LLM path keeps the feature."""
from datetime import datetime, timezone

from config.load import Thresholds
from normalize.field import (
    AssetRow, FieldSnapshot, InpBucketRow, PageTypeRow, SessionKpis,
)
from rag.retrieve import detect_field_symptoms

NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _snap(**kw):
    return FieldSnapshot(
        snapshot_id="s1", project="oakley", hosts=["www.oakley.com"],
        window_from=NOW, window_to=NOW, fetched_at=NOW, **kw,
    )


def test_no_data_yields_no_symptoms():
    assert detect_field_symptoms(_snap()) == []


def test_page_group_bounce_above_site_wide_by_the_margin_fires():
    snap = _snap(
        sessions=SessionKpis(bounce_pct=40.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=61.5)],
    )
    codes = [s.code for s in detect_field_symptoms(snap, page_group="Pdp")]
    assert "field_bounce_high" in codes


def test_bounce_within_the_margin_does_not_fire():
    snap = _snap(
        sessions=SessionKpis(bounce_pct=40.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=45.0)],
    )
    assert detect_field_symptoms(snap, page_group="Pdp") == []


def test_margin_is_percentage_points_not_a_ratio():
    """2% -> 4% doubles but is only 2pp: a low-traffic group must not scream."""
    snap = _snap(
        sessions=SessionKpis(bounce_pct=2.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=4.0)],
    )
    assert detect_field_symptoms(snap, page_group="Pdp") == []


def test_low_cache_hit_ratio_names_the_asset_type():
    snap = _snap(assets=[
        AssetRow(asset_type="Images", cache_hit_pct=48.0, origin_ms=900.0),
        AssetRow(asset_type="CSS", cache_hit_pct=96.0),
    ])
    symptoms = detect_field_symptoms(snap)
    assert any(s.code == "field_cache_low" and "Images" in s.text
               for s in symptoms)
    assert not any("CSS" in s.text for s in symptoms)


def test_frustration_severity_splits_at_the_configured_thresholds():
    warn = detect_field_symptoms(
        _snap(by_pagetype=[PageTypeRow(page_group="Pdp", frustration_p75=40.0)]),
        page_group="Pdp",
    )
    fail = detect_field_symptoms(
        _snap(by_pagetype=[PageTypeRow(page_group="Pdp", frustration_p75=70.0)]),
        page_group="Pdp",
    )
    assert [s.severity for s in warn if s.code.startswith("field_frustration")] == ["warn"]
    assert [s.severity for s in fail if s.code.startswith("field_frustration")] == ["fail"]


def test_worst_inp_bucket_with_climbing_frustration_fires():
    snap = _snap(inp_buckets=[
        InpBucketRow(bucket="1 - under 200 ms (good)", beacons=900, avg_frustration=12.0),
        InpBucketRow(bucket="4 - over 1000 ms (critical)", beacons=300, avg_frustration=71.0),
    ])
    codes = [s.code for s in detect_field_symptoms(snap)]
    assert "field_inp_frustration" in codes


def test_symptoms_are_ordered_fail_before_warn_and_deterministic():
    snap = _snap(
        sessions=SessionKpis(bounce_pct=30.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=70.0,
                                 frustration_p75=70.0)],
        assets=[AssetRow(asset_type="Images", cache_hit_pct=10.0)],
    )
    first = detect_field_symptoms(snap, page_group="Pdp")
    assert [s.severity for s in first] == sorted(
        [s.severity for s in first], key=lambda sev: 0 if sev == "fail" else 1
    )
    assert [s.code for s in first] == [
        s.code for s in detect_field_symptoms(snap, page_group="Pdp")
    ]


def test_custom_thresholds_are_honoured():
    snap = _snap(
        sessions=SessionKpis(bounce_pct=40.0),
        by_pagetype=[PageTypeRow(page_group="Pdp", bounce_pct=45.0)],
    )
    th = Thresholds(field_bounce_excess_pp=2.0)
    codes = [s.code for s in detect_field_symptoms(snap, page_group="Pdp",
                                                   thresholds=th)]
    assert "field_bounce_high" in codes
