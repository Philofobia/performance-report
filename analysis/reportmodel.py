"""The Report JSON — the contract between analysis and rendering (§6, §6.2).

The skeleton never changes; only the data does. That guarantee is enforced
here rather than in the template: this module authors a fully-ordered document
and Phase 5's template is a pure transform over it, computing nothing. Every
list has an explicit total order, so two campaigns over identical data produce
identical JSON — apart from ``cover.generated_at``, which ``stable_payload``
strips for the determinism check.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from analysis.estimator import Projection
from analysis.findings import PageAnalysis
from analysis.trends import TrendSeries
from config.load import Settings
from normalize.schema import Run

SCHEMA_VERSION = 1

_SEVERITY_RANK = {"pass": 0, "warn": 1, "fail": 2}


def _slug(value: str) -> str:
    """Lowercase, non-alphanumerics collapsed to '-' — same rule as knowledge.py.

    This reaches the filesystem as a directory name, so it is a safety
    requirement, not cosmetics (SECURITY_PLAN §2).
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "report"


def campaign_id(project: str, run_ids: Sequence[str]) -> str:
    """Content-addressed campaign identity.

    Derived from the runs, not the clock, so the determinism test can run the
    pipeline twice and compare the outputs directly.
    """
    digest = hashlib.sha256("\n".join(sorted(run_ids)).encode("utf-8")).hexdigest()
    return f"{_slug(project)}-{digest[:8]}"


def verdict_for(symptoms: Sequence[Any]) -> str:
    """pass / warn / fail from the severities present."""
    severities = {s.severity for s in symptoms}
    if "fail" in severities:
        return "fail"
    if "warn" in severities:
        return "warn"
    return "pass"


# --------------------------------------------------------------------------- #
# Models — one per section of the fixed skeleton
# --------------------------------------------------------------------------- #
class ProjectionModel(BaseModel):
    metric: str
    before: float
    after_low: float
    after_high: float
    reduction_pct: float
    source: str

    @classmethod
    def of(cls, projection: Projection) -> "ProjectionModel":
        return cls(
            metric=projection.metric, before=projection.before,
            after_low=projection.after_low, after_high=projection.after_high,
            reduction_pct=projection.reduction_pct, source=projection.source,
        )


class TrendPointModel(BaseModel):
    run_id: str
    value: float
    at: str


class TrendSeriesModel(BaseModel):
    """One metric's history under one condition (PROJECT_SPEC §10 Phase 7).

    ``direction`` is ``improved`` / ``regressed`` / ``flat`` / ``new``, and
    ``crossed`` is ``into_fail`` / ``into_pass`` / null. They are separate
    because they answer different questions: a metric can improve
    substantially and still be over its target.
    """

    page: str
    device: str
    network: str
    metric: str
    points: List[TrendPointModel] = Field(default_factory=list)
    direction: str = "new"
    delta_pct: Optional[float] = None
    target: Optional[float] = None
    crossed: Optional[str] = None

    @classmethod
    def of(cls, series: TrendSeries) -> "TrendSeriesModel":
        return cls(
            page=series.page, device=series.device, network=series.network,
            metric=series.metric,
            points=[
                TrendPointModel(run_id=p.run_id, value=p.value, at=p.at)
                for p in series.points
            ],
            direction=series.direction, delta_pct=series.delta_pct,
            target=series.target, crossed=series.crossed,
        )


class Cover(BaseModel):
    project: str
    campaign_id: str
    generated_at: datetime
    pages: List[str]
    verdict: str


class Summary(BaseModel):
    problem: str
    key_finding: str
    top_actions: List[str]


class SymptomModel(BaseModel):
    code: str
    text: str
    severity: str
    metric: Optional[str] = None
    value: Optional[float] = None
    target: Optional[float] = None


class ResourceModel(BaseModel):
    name: str
    type: str
    transfer_kb: float
    duration_ms: float


class ConditionRow(BaseModel):
    run_id: str
    device: str
    network: str
    cpu_throttle: float
    runs: int
    metrics: Dict[str, Optional[float]]


class FindingModel(BaseModel):
    title: str
    detail: str = ""
    evidence: List[str] = Field(default_factory=list)
    symptom_codes: List[str] = Field(default_factory=list)


class ImpactModel(BaseModel):
    audience: str
    text: str


class RecommendationModel(BaseModel):
    title: str
    rationale: str = ""
    playbook_source: str
    playbook_section: str = ""
    effort: str
    magnitude: str
    projections: List[ProjectionModel] = Field(default_factory=list)


class PageBlock(BaseModel):
    name: str
    url: str
    primary_run_id: str
    verdict: str
    conditions: List[ConditionRow]
    metrics: Dict[str, Any]
    targets: Dict[str, float]
    #: Defaulted so a `report.json` written before Phase 7 still validates.
    trends: List[TrendSeriesModel] = Field(default_factory=list)
    symptoms: List[SymptomModel]
    resources: List[ResourceModel]
    resource_type_totals: Dict[str, float]
    summary: str
    findings: List[FindingModel]
    impacts: List[ImpactModel]
    recommendations: List[RecommendationModel]
    projections: Dict[str, ProjectionModel]


class ComparisonRow(BaseModel):
    page: str
    device: str
    network: str
    lcp_ms: Optional[float] = None
    cls: Optional[float] = None
    inp_ms: Optional[float] = None
    tbt_ms: Optional[float] = None
    verdict: str


class CaptureRow(BaseModel):
    page: str
    run_id: str
    screenshot: Optional[str] = None
    har: Optional[str] = None
    trace: Optional[str] = None


class Methodology(BaseModel):
    devices: List[str]
    networks: List[str]
    runs_per_condition: List[int]
    captures: List[CaptureRow]
    thresholds: Dict[str, float]


class ReportMeta(BaseModel):
    analysis_mode: str
    degradation_reason: Optional[str] = None
    model: str
    playbooks_cited: List[str] = Field(default_factory=list)
    dropped_recommendations: int = 0
    knowledge_digest: str = ""


class Report(BaseModel):
    schema_version: int = SCHEMA_VERSION
    cover: Cover
    summary: Summary
    pages: List[PageBlock]
    comparison: List[ComparisonRow]
    methodology: Methodology
    meta: ReportMeta


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _condition_row(run: Run) -> ConditionRow:
    cwp = run.metrics.cwp
    return ConditionRow(
        run_id=run.run_id,
        device=run.condition.device,
        network=run.condition.network,
        cpu_throttle=run.condition.cpu_throttle,
        runs=run.condition.runs,
        metrics={
            "lcp_ms": cwp.lcp_ms, "cls": cwp.cls, "inp_ms": cwp.inp_ms,
            "fcp_ms": cwp.fcp_ms, "ttfb_ms": cwp.ttfb_ms, "tbt_ms": cwp.tbt_ms,
        },
    )


def _page_block(
    page: PageAnalysis,
    settings: Settings,
    trends: Sequence[TrendSeries] = (),
) -> PageBlock:
    run = page.primary_run
    resources = sorted(
        run.resource_timings, key=lambda t: (-(t.transfer_kb or 0.0), t.name)
    )
    totals: Dict[str, float] = {}
    for timing in run.resource_timings:
        totals[timing.type] = totals.get(timing.type, 0.0) + (timing.transfer_kb or 0.0)

    th = settings.thresholds
    return PageBlock(
        name=page.page_name,
        url=page.page_url,
        primary_run_id=run.run_id,
        verdict=verdict_for(page.symptoms),
        conditions=[_condition_row(r) for r in page.runs],
        metrics=run.metrics.model_dump(mode="json"),
        targets={"lcp_ms": float(th.lcp_good_ms), "cls": float(th.cls_good),
                 "inp_ms": float(th.inp_good_ms), "fcp_ms": float(th.fcp_good_ms),
                 "ttfb_ms": float(th.ttfb_good_ms)},
        trends=[TrendSeriesModel.of(series) for series in trends],
        symptoms=[
            SymptomModel(code=s.code, text=s.text, severity=s.severity,
                         metric=s.metric, value=s.value, target=s.target)
            for s in page.symptoms
        ],
        resources=[
            ResourceModel(name=t.name, type=t.type, transfer_kb=t.transfer_kb,
                          duration_ms=t.duration_ms)
            for t in resources
        ],
        resource_type_totals={k: totals[k] for k in sorted(totals)},
        summary=page.summary,
        findings=[
            FindingModel(title=f.title, detail=f.detail, evidence=list(f.evidence),
                         symptom_codes=list(f.symptom_codes))
            for f in page.findings
        ],
        impacts=[ImpactModel(audience=i.audience, text=i.text) for i in page.impacts],
        recommendations=[
            RecommendationModel(
                title=r.title, rationale=r.rationale,
                playbook_source=r.playbook_source,
                playbook_section=r.playbook_section, effort=r.effort,
                magnitude="estimated" if r.projections else "unknown",
                projections=[ProjectionModel.of(p) for p in r.projections],
            )
            for r in page.recommendations
        ],
        projections={
            metric: ProjectionModel.of(projection)
            for metric, projection in sorted(page.projections.items())
        },
    )


def _comparison(
    pages: Sequence[PageAnalysis], settings: Settings
) -> List[ComparisonRow]:
    """One row per condition, each judged on *its own* measurements.

    The page verdict comes from its worst condition, but a comparison table
    exists precisely to show that desktop passed where mobile failed. Stamping
    the page verdict on every row would erase the thing the table is for.
    """
    from rag.retrieve import detect_symptoms

    rows: List[ComparisonRow] = []
    for page in pages:
        for run in page.runs:
            cwp = run.metrics.cwp
            rows.append(ComparisonRow(
                page=page.page_name, device=run.condition.device,
                network=run.condition.network, lcp_ms=cwp.lcp_ms, cls=cwp.cls,
                inp_ms=cwp.inp_ms, tbt_ms=cwp.tbt_ms,
                verdict=verdict_for(detect_symptoms(run, settings.thresholds)),
            ))
    return sorted(rows, key=lambda r: (r.page, r.device, r.network))


def _methodology(pages: Sequence[PageAnalysis], settings: Settings) -> Methodology:
    devices, networks, run_counts = set(), set(), set()
    captures: List[CaptureRow] = []
    for page in pages:
        for run in page.runs:
            devices.add(run.condition.device)
            networks.add(run.condition.network)
            run_counts.add(run.condition.runs)
            captures.append(CaptureRow(
                page=page.page_name, run_id=run.run_id,
                screenshot=run.captures.screenshot, har=run.captures.har,
                trace=run.captures.trace,
            ))
    th = settings.thresholds
    return Methodology(
        devices=sorted(devices),
        networks=sorted(networks),
        runs_per_condition=sorted(run_counts),
        captures=sorted(captures, key=lambda c: (c.page, c.run_id)),
        thresholds={k: float(v) for k, v in sorted(th.model_dump().items())},
    )


def build_report(
    pages: Sequence[PageAnalysis],
    *,
    project: str,
    settings: Settings,
    summary: Any,
    generated_at: datetime,
    model: str,
    knowledge_digest: str = "",
    trends: Optional[Mapping[str, Sequence[TrendSeries]]] = None,
) -> Report:
    """Assemble the Report JSON from per-page analyses.

    ``summary`` is anything with ``problem``, ``key_finding`` and
    ``top_actions`` — an ``LlmSummary`` or the rule-based stand-in.

    ``trends`` is keyed by page name. A page absent from it renders the trend
    section's empty state; no section is ever conditionally omitted.
    """
    trends = trends or {}
    ordered = sorted(pages, key=lambda p: p.page_name)
    run_ids = [run.run_id for page in ordered for run in page.runs]

    degraded = [p for p in ordered if p.mode != "llm"]
    return Report(
        schema_version=SCHEMA_VERSION,
        cover=Cover(
            project=project,
            campaign_id=campaign_id(project, run_ids),
            generated_at=generated_at,
            pages=[p.page_name for p in ordered],
            verdict=max(
                (verdict_for(p.symptoms) for p in ordered),
                key=lambda v: _SEVERITY_RANK[v],
                default="pass",
            ),
        ),
        summary=Summary(
            problem=summary.problem,
            key_finding=summary.key_finding,
            top_actions=list(summary.top_actions),
        ),
        pages=[
            _page_block(p, settings, trends.get(p.page_name, ()))
            for p in ordered
        ],
        comparison=_comparison(ordered, settings),
        methodology=_methodology(ordered, settings),
        meta=ReportMeta(
            analysis_mode="rule_based" if degraded else "llm",
            degradation_reason=degraded[0].degradation_reason if degraded else None,
            model=model,
            playbooks_cited=sorted({s for p in ordered for s in p.playbooks_cited}),
            dropped_recommendations=sum(p.dropped_recommendations for p in ordered),
            knowledge_digest=knowledge_digest,
        ),
    )


def to_json(report: Report) -> str:
    """Serialise, stably indented, for writing to disk."""
    return json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False)


def stable_payload(report: Report) -> Dict[str, Any]:
    """The report minus the volatile fields — what the determinism test compares."""
    payload = report.model_dump(mode="json")
    payload["cover"].pop("generated_at", None)
    return payload
