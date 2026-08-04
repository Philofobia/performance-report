"""Unit tests for analysis/appendix.py — HAR reduction for the report appendix.

The reduction is what makes a multi-megabyte HAR into a table a reader can act
on, so the tests are about honesty: the top-N must be the actual top N, the
totals must describe the whole capture rather than the slice, and a HAR the
browser truncated must degrade rather than abort the report.
"""
from __future__ import annotations

import json

from analysis.appendix import (
    HarSummary,
    classify,
    entry_transfer_bytes,
    read_har,
    reduce_har,
    summarize_capture,
)


def an_entry(url="https://example.com/a.js", *, size=1000, mime="text/javascript",
             resource_type=None, status=200, time=12.5):
    entry = {
        "time": time,
        "request": {"url": url},
        "response": {
            "status": status,
            "_transferSize": size,
            "content": {"mimeType": mime},
        },
    }
    if resource_type is not None:
        entry["_resourceType"] = resource_type
    return entry


def a_har(entries):
    return {"log": {"version": "1.2", "entries": list(entries)}}


def test_rows_are_ordered_by_transfer_size_descending():
    har = a_har([
        an_entry("https://example.com/small.js", size=10),
        an_entry("https://example.com/huge.js", size=9000),
        an_entry("https://example.com/mid.js", size=500),
    ])
    rows = reduce_har(har, top_n=10).rows
    assert [r["url"] for r in rows] == [
        "https://example.com/huge.js",
        "https://example.com/mid.js",
        "https://example.com/small.js",
    ]


def test_equal_sizes_are_tie_broken_by_url_so_order_is_reproducible():
    har = a_har([
        an_entry("https://example.com/z.js", size=500),
        an_entry("https://example.com/a.js", size=500),
    ])
    rows = reduce_har(har, top_n=10).rows
    assert [r["url"] for r in rows] == [
        "https://example.com/a.js",
        "https://example.com/z.js",
    ]


def test_duplicate_size_and_url_entries_keep_a_stable_input_order_tiebreak():
    # A tracking pixel fired twice, or a duplicated script request: same URL,
    # same transfer size. Neither the size nor the URL tie-break separates
    # them, so the sort needs the entry's original position as a third,
    # self-contained component — otherwise "order" depends on how the HAR
    # happened to be serialized rather than on anything in the row itself.
    har = a_har([
        an_entry("https://example.com/dup.js", size=500, status=200),
        an_entry("https://example.com/dup.js", size=500, status=304),
    ])
    rows = reduce_har(har, top_n=10).rows
    assert [r["status"] for r in rows] == [200, 304]


def test_top_n_truncates_but_totals_describe_the_whole_capture():
    har = a_har([an_entry(f"https://example.com/{i}.js", size=100) for i in range(20)])
    summary = reduce_har(har, top_n=5)
    assert len(summary.rows) == 5
    assert summary.total_requests == 20
    assert summary.total_transfer_bytes == 2000


def test_negative_transfer_size_from_a_cache_hit_clamps_to_zero():
    # A HAR reports -1 for a served-from-cache response. A negative byte count
    # in a size table is worse than a zero.
    assert entry_transfer_bytes(an_entry(size=-1)) == 0


def test_transfer_size_falls_back_to_body_plus_headers():
    entry = {"request": {"url": "https://example.com/a.js"},
             "response": {"bodySize": 900, "headersSize": 100}}
    assert entry_transfer_bytes(entry) == 1000


def test_resource_type_prefers_playwrights_own_label():
    assert classify(an_entry(resource_type="stylesheet", mime="text/plain")) == "stylesheet"


def test_resource_type_falls_back_to_mime_then_extension_then_other():
    assert classify(an_entry(mime="image/png")) == "image"
    assert classify({"request": {"url": "https://example.com/x.woff2"},
                     "response": {}}) == "font"
    assert classify({"request": {"url": "https://example.com/x"},
                     "response": {}}) == "other"


def test_classify_tolerates_a_content_field_that_is_not_a_mapping():
    # A HAR that parses fine as JSON can still have "content" as null, a list,
    # or a bare scalar. Reaching for .get("mimeType") on any of those raises,
    # and classify() must never be the reason a well-formed HAR blows up the
    # report.
    for bad_content in (None, ["text/plain"], "text/plain"):
        entry = {"request": {"url": "https://example.com/x"},
                 "response": {"content": bad_content, "status": 200}}
        assert classify(entry) == "other"


def test_urls_are_re_redacted_even_though_the_stored_har_was_scrubbed():
    # A HAR written before a scrubbing rule existed is still in the store.
    har = a_har([an_entry("https://example.com/a.js?token=hunter2")])
    assert "hunter2" not in reduce_har(har, top_n=5).rows[0]["url"]


def test_missing_duration_is_none_not_zero():
    entry = {"request": {"url": "https://example.com/a.js"}, "response": {}}
    assert reduce_har(a_har([entry]), top_n=5).rows[0]["duration_ms"] is None


def test_a_har_with_no_entries_reduces_to_an_empty_summary():
    assert reduce_har(a_har([]), top_n=5) == HarSummary(
        rows=[], total_requests=0, total_transfer_bytes=0
    )


def test_a_har_missing_its_log_block_reduces_to_an_empty_summary():
    assert reduce_har({"not": "a har"}, top_n=5).total_requests == 0


def test_read_har_reports_a_missing_file_as_an_error_not_an_exception(tmp_path):
    har, error = read_har(tmp_path / "gone.har")
    assert har is None
    assert "not found" in error.lower()


def test_read_har_reports_malformed_json_with_the_parser_detail(tmp_path):
    path = tmp_path / "capture.har"
    path.write_text("{ truncated", encoding="utf-8")
    har, error = read_har(path)
    assert har is None
    assert "malformed" in error.lower()


def test_summarize_capture_records_the_har_digest_and_size(tmp_path):
    path = tmp_path / "capture.har"
    payload = json.dumps(a_har([an_entry(size=42)]))
    path.write_text(payload, encoding="utf-8")

    summary = summarize_capture(screenshot=None, har=str(path), top_n=5)
    assert summary.har_bytes == len(payload.encode("utf-8"))
    assert len(summary.har_sha256) == 64
    assert summary.total_transfer_bytes == 42


def test_summarize_capture_degrades_per_artifact_without_raising(tmp_path):
    summary = summarize_capture(screenshot=None, har=None, top_n=5)
    assert summary.degraded == ["screenshot not retained", "HAR not retained"]
    assert summary.requests == []


def test_summarize_capture_reports_a_path_that_points_at_nothing(tmp_path):
    summary = summarize_capture(
        screenshot=str(tmp_path / "gone.png"), har=str(tmp_path / "gone.har"), top_n=5
    )
    assert summary.degraded == ["screenshot file missing", "HAR file missing"]


def test_summarize_capture_reports_a_malformed_har_and_still_returns(tmp_path):
    path = tmp_path / "capture.har"
    path.write_text("{ truncated", encoding="utf-8")
    summary = summarize_capture(screenshot=None, har=str(path), top_n=5)
    assert any("malformed" in reason.lower() for reason in summary.degraded)
    assert summary.requests == []
