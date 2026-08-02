"""Pydantic canonical run object (PROJECT_SPEC §4.2/§4.3).

Every run — manual or automated — converges to :class:`Run`. Validation enforces
metric units/ranges (e.g. ``lcp_ms >= 0``, ``cls`` in 0..1, Lighthouse 0..100)
and, for ``automated`` runs, requires the CWV trio (LCP, CLS, INP).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class Project(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)


class PageRef(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)


class Condition(BaseModel):
    device: str = Field(min_length=1)
    network: str = Field(min_length=1)
    cpu_throttle: float = Field(default=1, ge=0)
    runs: int = Field(default=3, ge=1, le=100)


Source = Literal["automated", "manual", "mixed"]


class Meta(BaseModel):
    created_at: datetime
    source: Source
    runner: str = Field(default="")


class Problem(BaseModel):
    description: str = Field(default="", max_length=10000)
    keywords: List[str] = Field(default_factory=list)


class CwpMetrics(BaseModel):
    lcp_ms: Optional[float] = Field(default=None, ge=0)
    cls: Optional[float] = Field(default=None, ge=0, le=1)
    inp_ms: Optional[float] = Field(default=None, ge=0)
    fcp_ms: Optional[float] = Field(default=None, ge=0)
    ttfb_ms: Optional[float] = Field(default=None, ge=0)
    # Total Blocking Time — the lab responsiveness metric derived from long
    # tasks (DevTools/Lighthouse definition). Complements INP, which is a
    # field metric requiring a real interaction.
    tbt_ms: Optional[float] = Field(default=None, ge=0)
    target_lcp_ms: Optional[float] = Field(default=None, ge=0)
    target_cls: Optional[float] = Field(default=None, ge=0, le=1)
    target_inp_ms: Optional[float] = Field(default=None, ge=0)


class LighthouseScores(BaseModel):
    performance: Optional[int] = Field(default=None, ge=0, le=100)
    accessibility: Optional[int] = Field(default=None, ge=0, le=100)
    best_practices: Optional[int] = Field(default=None, ge=0, le=100)
    seo: Optional[int] = Field(default=None, ge=0, le=100)


class NetworkMetrics(BaseModel):
    total_transfer_kb: Optional[float] = Field(default=None, ge=0)
    request_count: Optional[int] = Field(default=None, ge=0)
    render_blocking_css: Optional[int] = Field(default=None, ge=0)


class MainThreadMetrics(BaseModel):
    """DevTools main-thread breakdown, read over CDP ``Performance.getMetrics``.

    Answers *where* the time went (script vs. layout vs. style) and how heavy
    the document is — the grounding for the report's "where the problem is"
    section. All fields optional: a counter the browser did not report stays
    ``None`` rather than being coerced to 0.
    """

    script_ms: Optional[float] = Field(default=None, ge=0)
    layout_ms: Optional[float] = Field(default=None, ge=0)
    style_ms: Optional[float] = Field(default=None, ge=0)
    task_ms: Optional[float] = Field(default=None, ge=0)
    js_heap_kb: Optional[float] = Field(default=None, ge=0)
    dom_nodes: Optional[int] = Field(default=None, ge=0)
    layout_count: Optional[int] = Field(default=None, ge=0)
    js_event_listeners: Optional[int] = Field(default=None, ge=0)
    resource_count: Optional[int] = Field(default=None, ge=0)


class Metrics(BaseModel):
    cwp: CwpMetrics = Field(default_factory=CwpMetrics)
    lighthouse: LighthouseScores = Field(default_factory=LighthouseScores)
    network: NetworkMetrics = Field(default_factory=NetworkMetrics)
    main_thread: MainThreadMetrics = Field(default_factory=MainThreadMetrics)


class ResourceTiming(BaseModel):
    name: str = Field(min_length=1)
    type: str = Field(default="other", min_length=1)
    transfer_kb: float = Field(default=0, ge=0)
    duration_ms: float = Field(default=0, ge=0)


class Captures(BaseModel):
    screenshot: Optional[str] = None
    har: Optional[str] = None
    trace: Optional[str] = None


class Run(BaseModel):
    run_id: str = Field(min_length=1)
    project: Project
    page: PageRef
    condition: Condition
    meta: Meta
    problem: Problem = Field(default_factory=Problem)
    metrics: Metrics = Field(default_factory=Metrics)
    resource_timings: List[ResourceTiming] = Field(default_factory=list)
    captures: Captures = Field(default_factory=Captures)

    @model_validator(mode="after")
    def _require_cwv_for_automated(self) -> "Run":
        """§4.3: automated runs must carry the LCP/CLS/INP trio."""
        if self.meta.source == "automated":
            cwp = self.metrics.cwp
            missing = [
                name
                for name, val in (
                    ("lcp_ms", cwp.lcp_ms),
                    ("cls", cwp.cls),
                    ("inp_ms", cwp.inp_ms),
                )
                if val is None
            ]
            if missing:
                raise ValueError(
                    "Automated runs require the CWV trio; missing: "
                    + ", ".join(missing)
                )
        return self
