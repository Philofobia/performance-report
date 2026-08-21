"""Build a retrieval query from a run and fetch grounding context (§5.2).

A raw metrics dict is a poor embedding query — numbers don't sit near prose in
embedding space. So a run is first turned into **symptoms**: rule-based,
threshold-driven statements like "LCP is 6200ms against a 2500ms target" and
"media transfers dominate page weight". Those read like the playbooks, which is
what makes retrieval land on the right one.

Symptom detection is deliberately rule-based, not model-based: it uses the
thresholds already in ``config/settings.yaml``, so it is deterministic,
explainable in the report, and testable without an LLM.

§5.3's concern — "LCP high + media heavy" should retrieve the *media* playbook
rather than the *fonts* one — is handled by including the dominant resource
type in the query, so weight is not carried by the metric names alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from config.load import Thresholds
from normalize.schema import Run
from store.vectordb import SearchHit

# Resource types we describe in prose when one dominates page weight.
_TYPE_PROSE = {
    "media": "video/media",
    "img": "images",
    "image": "images",
    "script": "JavaScript",
    "css": "CSS",
    "link": "CSS",
    "font": "fonts",
}


@dataclass
class Symptom:
    """One detected, threshold-backed problem statement."""

    code: str
    text: str
    severity: str = "warn"  # "fail" | "warn"
    metric: Optional[str] = None
    value: Optional[float] = None
    target: Optional[float] = None


@dataclass
class RetrievalQuery:
    """The query text plus the symptoms that produced it (report-explainable)."""

    text: str
    symptoms: List[Symptom] = field(default_factory=list)

    @property
    def codes(self) -> List[str]:
        return [s.code for s in self.symptoms]


def _fmt(value: float) -> str:
    """Render a metric without trailing noise (6200.0 -> 6200)."""
    return str(int(value)) if float(value).is_integer() else str(round(float(value), 3))


def detect_symptoms(run: Run, thresholds: Optional[Thresholds] = None) -> List[Symptom]:
    """Derive threshold-backed symptoms from a run's metrics.

    Ordered by severity then metric so the query text — and therefore
    retrieval — is deterministic for identical input (§6.2).
    """
    th = thresholds or Thresholds()
    cwp = run.metrics.cwp
    net = run.metrics.network
    mt = run.metrics.main_thread
    found: List[Symptom] = []

    def add(code, text, severity, metric=None, value=None, target=None):
        found.append(Symptom(code, text, severity, metric, value, target))

    if cwp.lcp_ms is not None:
        if cwp.lcp_ms > th.lcp_fail_ms:
            add("lcp_fail", f"Largest Contentful Paint is {_fmt(cwp.lcp_ms)}ms, far above the "
                f"{th.lcp_good_ms}ms target - the main content renders too slowly.",
                "fail", "lcp_ms", cwp.lcp_ms, th.lcp_good_ms)
        elif cwp.lcp_ms > th.lcp_good_ms:
            add("lcp_warn", f"Largest Contentful Paint is {_fmt(cwp.lcp_ms)}ms, above the "
                f"{th.lcp_good_ms}ms target.", "warn", "lcp_ms", cwp.lcp_ms, th.lcp_good_ms)

    if cwp.cls is not None:
        if cwp.cls > th.cls_fail:
            add("cls_fail", f"Cumulative Layout Shift is {_fmt(cwp.cls)}, well above the "
                f"{th.cls_good} target - the layout moves while loading.",
                "fail", "cls", cwp.cls, th.cls_good)
        elif cwp.cls > th.cls_good:
            add("cls_warn", f"Cumulative Layout Shift is {_fmt(cwp.cls)}, above the "
                f"{th.cls_good} target.", "warn", "cls", cwp.cls, th.cls_good)

    if cwp.inp_ms is not None:
        if cwp.inp_ms > th.inp_fail_ms:
            add("inp_fail", f"Interaction to Next Paint is {_fmt(cwp.inp_ms)}ms against a "
                f"{th.inp_good_ms}ms target - the page responds slowly to input.",
                "fail", "inp_ms", cwp.inp_ms, th.inp_good_ms)
        elif cwp.inp_ms > th.inp_good_ms:
            add("inp_warn", f"Interaction to Next Paint is {_fmt(cwp.inp_ms)}ms, above the "
                f"{th.inp_good_ms}ms target.", "warn", "inp_ms", cwp.inp_ms, th.inp_good_ms)

    if cwp.ttfb_ms is not None and cwp.ttfb_ms > th.ttfb_good_ms:
        add("ttfb_slow", f"Time to First Byte is {_fmt(cwp.ttfb_ms)}ms against a "
            f"{th.ttfb_good_ms}ms target - the server responds slowly.",
            "warn", "ttfb_ms", cwp.ttfb_ms, th.ttfb_good_ms)

    if cwp.fcp_ms is not None and cwp.fcp_ms > th.fcp_good_ms:
        add("fcp_slow", f"First Contentful Paint is {_fmt(cwp.fcp_ms)}ms against a "
            f"{th.fcp_good_ms}ms target.", "warn", "fcp_ms", cwp.fcp_ms, th.fcp_good_ms)

    # Main-thread work: TBT is the lab responsiveness signal.
    if cwp.tbt_ms is not None and cwp.tbt_ms > th.tbt_good_ms:
        add("tbt_high", f"Total Blocking Time is {_fmt(cwp.tbt_ms)}ms - long tasks block the "
            "main thread and delay interactivity.",
            "fail" if cwp.tbt_ms > th.tbt_fail_ms else "warn",
            "tbt_ms", cwp.tbt_ms, th.tbt_good_ms)

    if mt.script_ms is not None and mt.task_ms and mt.script_ms > 0.5 * mt.task_ms:
        add("script_heavy", f"JavaScript execution accounts for {_fmt(mt.script_ms)}ms of "
            f"{_fmt(mt.task_ms)}ms total main-thread time - script cost dominates.",
            "warn", "script_ms", mt.script_ms, None)

    if net.render_blocking_css:
        add("render_blocking_css", f"{net.render_blocking_css} render-blocking stylesheets delay "
            "first paint.", "warn", "render_blocking_css", float(net.render_blocking_css), 0)

    if net.total_transfer_kb is not None and net.total_transfer_kb > 2000:
        add("page_weight", f"Total transfer is {_fmt(net.total_transfer_kb)}KB - the page is heavy.",
            "warn", "total_transfer_kb", net.total_transfer_kb, 2000)

    if net.request_count is not None and net.request_count > 80:
        add("many_requests", f"The page issues {net.request_count} requests.",
            "warn", "request_count", float(net.request_count), 80)

    # Dominant resource type — §5.3: steer retrieval to the right playbook.
    heaviest = dominant_resource_type(run)
    if heaviest:
        kind, share = heaviest
        add("dominant_" + kind, f"{_TYPE_PROSE.get(kind, kind)} account for "
            f"{int(share * 100)}% of transferred bytes.", "warn", None, None, None)

    order = {"fail": 0, "warn": 1}
    return sorted(found, key=lambda s: (order.get(s.severity, 2), s.code))


def dominant_resource_type(run: Run, *, min_share: float = 0.35) -> Optional[tuple]:
    """The resource type carrying most bytes, if it exceeds ``min_share``."""
    totals: Dict[str, float] = {}
    for timing in run.resource_timings:
        totals[timing.type] = totals.get(timing.type, 0.0) + (timing.transfer_kb or 0.0)
    grand = sum(totals.values())
    if grand <= 0:
        return None
    kind, kb = max(totals.items(), key=lambda kv: (kv[1], kv[0]))
    share = kb / grand
    return (kind, share) if share >= min_share else None


def build_query(
    run: Run,
    *,
    thresholds: Optional[Thresholds] = None,
    include_problem: bool = True,
    max_problem_chars: int = 1000,
) -> RetrievalQuery:
    """Turn a run into a prose retrieval query plus its symptom list.

    The user's free-text problem description is included (truncated per
    SECURITY_PLAN §2.4) because it often names the cause directly — but it is
    appended as *context*, never as instructions (§2.3 handles that at prompt
    build time).
    """
    symptoms = detect_symptoms(run, thresholds)
    parts: List[str] = [
        f"Web performance problems on the {run.page.name} page "
        f"({run.condition.device} device, {run.condition.network} network)."
    ]
    parts.extend(s.text for s in symptoms)

    if include_problem and run.problem.description:
        parts.append("Reported symptom: " + run.problem.description[:max_problem_chars])

    return RetrievalQuery(text="\n".join(parts), symptoms=symptoms)


def retrieve_context(
    run: Run,
    store,
    client,
    *,
    thresholds: Optional[Thresholds] = None,
    top_k: int = 5,
    kind: Optional[str] = "knowledge",
    min_score: Optional[float] = None,
) -> tuple:
    """Build the query, embed it, and return ``(hits, query)``.

    The query object comes back alongside the hits so the report can show *why*
    a playbook was retrieved — the symptoms are auditable, not a black box.
    """
    query = build_query(run, thresholds=thresholds)
    vector = client.embed_query(query.text)
    hits = store.query(vector, k=top_k, kind=kind, min_score=min_score)
    return hits, query


def retrieve_prior_findings(
    run: Run,
    store,
    client,
    *,
    thresholds: Optional[Thresholds] = None,
    top_k: int = 3,
    min_score: Optional[float] = None,
) -> List[SearchHit]:
    """Retrieve similar findings from previous runs (§5.1.2 — 'remember')."""
    query = build_query(run, thresholds=thresholds)
    vector = client.embed_query(query.text)
    return store.query(vector, k=top_k, kind="finding", min_score=min_score)
