"""Per-page analysis: the LLM path and the path that works without one.

Two things live here that are easy to conflate and must not be:

* **Selection and validation** — which run represents a page, and whether a
  model's citation refers to a playbook that was actually retrieved. Rules,
  not judgement.
* **The rule-based path** — a complete analysis with no model at all, built
  from detected symptoms and front-matter symptom matching. It exists because
  a missing key or an exhausted free-tier quota should degrade the report, not
  destroy the campaign.

The rule-based path is not a stub. It reads as a competent threshold report;
what it cannot do is reason about a page's specific architecture. The report
says which mode produced it, so nobody has to guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from analysis.estimator import Candidate, Projection, by_source, effort_of, project
from normalize.schema import Run
from rag.knowledge import Chunk
from rag.retrieve import Symptom

# At most this many recommendations from the rule-based path: enough to act on,
# few enough that the report stays a report rather than a checklist dump.
MAX_RULE_BASED_RECOMMENDATIONS = 6
MAX_TACTICS_PER_PLAYBOOK = 2

# Symptom code prefix -> what it costs, per audience. Fixed text: this is the
# rule-based substitute for the model's impact statements, and §6.2 forbids
# layout-level variation.
_IMPACT_LIBRARY: Dict[str, Dict[str, str]] = {
    "lcp": {
        "ux": "The main content appears late, so the page looks broken or empty "
              "during the wait.",
        "seo": "Largest Contentful Paint is a ranking signal; a failing value "
               "weakens search performance on mobile.",
        "business": "Slow main-content paint is strongly associated with "
                    "abandonment before the page becomes usable.",
    },
    "cls": {
        "ux": "Content moves under the reader, causing mis-taps and lost reading "
              "position.",
        "seo": "Cumulative Layout Shift is a ranking signal and a failing value "
               "counts against the page.",
        "business": "Shifting layouts cause accidental clicks on the wrong "
                    "control, including away from checkout.",
    },
    "inp": {
        "ux": "The page feels unresponsive: taps and clicks visibly lag.",
        "seo": "Interaction to Next Paint is a ranking signal and a failing value "
               "counts against the page.",
        "business": "Input lag on interactive controls interrupts task completion.",
    },
    "tbt": {
        "ux": "Long tasks block the main thread, so the page is visible before it "
              "is usable.",
        "seo": "Main-thread blocking degrades the responsiveness signals search "
               "engines measure.",
        "business": "A page that looks ready but ignores input reads as broken.",
    },
    "ttfb": {
        "ux": "Nothing can render until the server responds, so every other "
              "metric inherits the delay.",
        "seo": "Slow server response reduces crawl efficiency and delays every "
               "paint metric.",
        "business": "Server latency is paid by every visitor on every page view.",
    },
    "page_weight": {
        "ux": "A heavy page is slow on real mobile connections regardless of "
              "device speed.",
        "seo": "Page weight drives the paint metrics that search engines score.",
        "business": "Transfer volume is a direct cost on metered mobile data.",
    },
}

_DEFAULT_IMPACTS = {
    "ux": "Measured values exceed the configured targets, so the page is slower "
          "than intended for real users.",
    "seo": "Core Web Vitals outside their thresholds weaken search performance.",
    "business": "Slower pages convert worse than faster ones on the same traffic.",
}

_METRIC_NAMES = {
    "lcp_ms": "Largest Contentful Paint",
    "cls": "Cumulative Layout Shift",
    "inp_ms": "Interaction to Next Paint",
    "fcp_ms": "First Contentful Paint",
    "ttfb_ms": "Time to First Byte",
    "tbt_ms": "Total Blocking Time",
    "total_transfer_kb": "Page weight",
    "request_count": "Request count",
    "render_blocking_css": "Render-blocking CSS",
    "script_ms": "Script execution time",
}


@dataclass(frozen=True)
class Finding:
    """One localized problem statement."""

    title: str
    detail: str = ""
    #: Plain-language consequence for a reader who is not an engineer.
    consequence: str = ""
    evidence: Tuple[str, ...] = ()
    symptom_codes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Impact:
    """What a finding costs, for one audience."""

    audience: str
    text: str


@dataclass(frozen=True)
class Recommendation:
    """One action, bound to the playbook that justifies it."""

    title: str
    rationale: str
    playbook_source: str
    playbook_section: str
    effort: str
    #: Plain-language case for doing it, for whoever funds the work. Empty on
    #: the rule-based path, which has no author to write one.
    why_it_matters: str = ""
    projections: Tuple[Projection, ...] = ()


def select_primary(runs: Sequence[Run]) -> Run:
    """The run that best represents a page: its worst condition.

    A page tested on mobile/slow-4g and desktop/fast-3g has two truths. The
    report analyses the worse one in depth and shows the other for comparison,
    because a recommendation derived from the easy condition is the wrong
    recommendation.

    Ordering is a total order (fail count, LCP, run_id), so the choice never
    depends on the order the caller happened to pass.
    """
    if not runs:
        raise ValueError("select_primary requires at least one run")

    from rag.retrieve import detect_symptoms

    def key(run: Run):
        fails = sum(1 for s in detect_symptoms(run) if s.severity == "fail")
        lcp = run.metrics.cwp.lcp_ms
        return (-fails, -(lcp if lcp is not None else -1.0), run.run_id)

    return sorted(runs, key=key)[0]


def _symptom_list(chunk: Chunk) -> List[str]:
    """Front-matter ``symptoms:`` as a list, whatever shape it was written in."""
    raw = chunk.metadata.get("symptoms", [])
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw] if isinstance(raw, (list, tuple)) else []


def match_playbooks_by_symptoms(
    symptom_codes: Sequence[str], chunks: Sequence[Chunk]
) -> List[Chunk]:
    """Select playbook chunks whose front matter claims one of these symptoms.

    This is retrieval without embeddings — the fallback used when there is no
    API key, and therefore no way to embed a query. Front matter already
    declares which symptoms a playbook addresses, so the mapping is exact
    rather than approximate, and free.
    """
    wanted = set(symptom_codes)
    matched = [c for c in chunks if wanted & set(_symptom_list(c))]
    return sorted(matched, key=lambda c: (c.source, c.chunk_id))


def _tactics(chunks: Sequence[Chunk]) -> List[Chunk]:
    """Chunks that are an actual tactic (a sub-heading), not a file preamble."""
    return [c for c in chunks if len(c.heading_path) >= 2]


def _first_paragraph(text: str, limit: int = 400) -> str:
    """The body's first paragraph, minus the heading trail prefix."""
    body = text.split("\n\n", 1)
    paragraph = body[1] if len(body) > 1 else body[0]
    cleaned = " ".join(paragraph.split())
    return cleaned[:limit]


def _evidence(symptom) -> str:
    """One measurement, rounded the way the report shows it everywhere else.

    Built as a string here, long before any renderer sees it, so the shared
    formatter has to be applied at the point of construction — otherwise
    ``lcp_ms=3439.7000000029802`` reaches the reader intact.
    """
    from report.glossary import load_glossary

    return f"{symptom.metric}={load_glossary().format_value(symptom.metric, symptom.value)}"


def _finding_title(symptom: Symptom) -> str:
    """A short title for a symptom-derived finding."""
    if symptom.metric in _METRIC_NAMES:
        verb = "exceeds its target" if symptom.severity == "fail" else "is above target"
        return f"{_METRIC_NAMES[symptom.metric]} {verb}"
    return symptom.code.replace("_", " ").capitalize()


def _impacts_for(symptoms: Sequence[Symptom]) -> List[Impact]:
    """One impact statement per audience, from the most severe symptom.

    Fixed text per audience keeps §6.2's determinism: the *layout* of section 4
    never varies, only which library entry it draws.
    """
    for symptom in symptoms:
        for prefix, library in _IMPACT_LIBRARY.items():
            if symptom.code.startswith(prefix):
                return [
                    Impact(audience=a, text=library[a])
                    for a in ("ux", "seo", "business")
                ]
    return [
        Impact(audience=a, text=_DEFAULT_IMPACTS[a])
        for a in ("ux", "seo", "business")
    ]


def _metrics_map(run: Run) -> Dict[str, Optional[float]]:
    """The metric values the estimator projects against."""
    cwp = run.metrics.cwp
    return {
        "lcp_ms": cwp.lcp_ms,
        "cls": cwp.cls,
        "inp_ms": cwp.inp_ms,
        "fcp_ms": cwp.fcp_ms,
        "ttfb_ms": cwp.ttfb_ms,
        "tbt_ms": cwp.tbt_ms,
        "total_transfer_kb": run.metrics.network.total_transfer_kb,
    }


def build_recommendations(
    rows: Sequence[Tuple[str, str, str, str, Mapping[str, Any]]],
    projections: Mapping[str, Sequence[Projection]],
) -> List[Recommendation]:
    """Assemble ordered recommendations from ``(title, rationale, source,
    section, metadata, why_it_matters)`` rows and projections grouped by source.

    Shared by both paths so LLM-authored and rule-based recommendations are
    ordered by exactly the same rule (§7.1). ``why_it_matters`` is the model's
    plain-language case for the work, empty on the rule-based path — which has
    no author to write one.
    """
    from analysis.estimator import rank_key

    built = [
        Recommendation(
            title=title,
            rationale=rationale,
            why_it_matters=why_it_matters,
            playbook_source=source,
            playbook_section=section,
            effort=effort_of(metadata),
            projections=tuple(projections.get(source, ())),
        )
        for title, rationale, source, section, metadata, why_it_matters in rows
    ]
    return sorted(
        built, key=lambda r: rank_key(r.playbook_source, r.title, r.projections)
    )


def _rule_based_recommendations(
    run: Run, symptoms: Sequence[Symptom], chunks: Sequence[Chunk]
) -> List[Recommendation]:
    """Tactics from the playbooks whose front matter names a detected symptom."""
    matched = match_playbooks_by_symptoms([s.code for s in symptoms], chunks)

    per_source: Dict[str, List[Chunk]] = {}
    for chunk in _tactics(matched):
        per_source.setdefault(chunk.source, []).append(chunk)

    selected: List[Chunk] = []
    for source in sorted(per_source):
        selected.extend(per_source[source][:MAX_TACTICS_PER_PLAYBOOK])
    selected = selected[:MAX_RULE_BASED_RECOMMENDATIONS]

    candidates = [
        Candidate(source=chunk.source, metadata=chunk.metadata) for chunk in selected
    ]
    projections = by_source(project(candidates, _metrics_map(run)))

    return build_recommendations(
        [
            (chunk.heading_path[-1], _first_paragraph(chunk.text),
             chunk.source, chunk.heading_path[-1], chunk.metadata, "")
            for chunk in selected
        ],
        projections,
    )


def rule_based_analysis(
    run: Run,
    symptoms: Sequence[Symptom],
    chunks: Sequence[Chunk],
) -> Tuple[str, List[Finding], List[Impact], List[Recommendation]]:
    """A complete analysis with no model involved.

    Findings restate detected symptoms; impacts come from a fixed library;
    recommendations are the tactics of the playbooks whose front matter names
    those symptoms. Every number still comes from the estimator.
    """
    if not symptoms:
        summary = (
            f"No threshold was exceeded on {run.page.name} under "
            f"{run.condition.device}/{run.condition.network}. All measured "
            "metrics are within their configured targets."
        )
        return summary, [], [], []

    worst = symptoms[0]
    summary = (
        f"{len(symptoms)} threshold issue(s) detected on {run.page.name} under "
        f"{run.condition.device}/{run.condition.network}. The most severe is: "
        f"{worst.text}"
    )

    findings = [
        Finding(
            title=_finding_title(symptom),
            detail=symptom.text,
            evidence=(_evidence(symptom),) if symptom.metric else (),
            symptom_codes=(symptom.code,),
        )
        for symptom in symptoms
    ]

    impacts = _impacts_for(symptoms)
    recommendations = _rule_based_recommendations(run, symptoms, chunks)
    return summary, findings, impacts, recommendations


@dataclass
class PageAnalysis:
    """Everything the report needs about one page."""

    page_name: str
    page_url: str
    primary_run: Run
    runs: List[Run]
    symptoms: List[Symptom]
    summary: str
    findings: List[Finding]
    impacts: List[Impact]
    recommendations: List[Recommendation]
    projections: Dict[str, Projection]
    mode: str = "rule_based"
    degradation_reason: Optional[str] = None
    dropped_recommendations: int = 0
    playbooks_cited: List[str] = field(default_factory=list)


def _rule_based_page(
    runs: List[Run],
    primary: Run,
    symptoms: List[Symptom],
    chunks: Sequence[Chunk],
    reason: Optional[str],
    dropped: int = 0,
) -> PageAnalysis:
    """Build a PageAnalysis from the no-model path."""
    from analysis.estimator import aggregate

    summary, findings, impacts, recommendations = rule_based_analysis(
        primary, symptoms, chunks
    )
    flat = [p for rec in recommendations for p in rec.projections]
    return PageAnalysis(
        page_name=primary.page.name,
        page_url=primary.page.url,
        primary_run=primary,
        runs=runs,
        symptoms=list(symptoms),
        summary=summary,
        findings=findings,
        impacts=impacts,
        recommendations=recommendations,
        projections=aggregate(flat, _metrics_map(primary)),
        mode="rule_based",
        degradation_reason=reason,
        dropped_recommendations=dropped,
        playbooks_cited=sorted({r.playbook_source for r in recommendations}),
    )


def analyze_page(
    runs: Sequence[Run],
    *,
    hits: Sequence[Any],
    symptoms: Sequence[Symptom],
    client: Optional[Any] = None,
    prior_findings: Sequence[Any] = (),
    chunks: Optional[Sequence[Chunk]] = None,
    knowledge_dir: str = "data/knowledge",
    no_client_reason: str = "no_api_key",
) -> PageAnalysis:
    """Analyse one page, with a model when there is one and rules when not.

    ``hits`` are the retrieved playbook chunks; their ``source`` values are the
    *only* citations a recommendation may claim. ``chunks`` is the on-disk
    playbook corpus used by the fallback — passed in so tests and repeated
    pages do not re-read the directory.

    ``no_client_reason`` distinguishes *why* there is no model. "No key
    configured" and "the user passed --no-llm" both land on the rule-based
    path, but the report must not claim the first when the second happened.
    """
    from analysis.estimator import aggregate
    from analysis.llm import (
        AnalysisError,
        InvalidModelOutputError,
        LlmUnavailableError,
    )
    from rag.budget import BudgetExhaustedError
    from rag.embeddings import EmbeddingError, MissingApiKeyError, QuotaExceededError
    from rag.knowledge import load_knowledge_dir
    from rag.prompt import build_analysis_prompt

    ordered_runs = sorted(
        runs, key=lambda r: (r.condition.device, r.condition.network, r.run_id)
    )
    primary = select_primary(ordered_runs)
    corpus = list(chunks) if chunks is not None else load_knowledge_dir(knowledge_dir)

    if client is None:
        return _rule_based_page(
            ordered_runs, primary, list(symptoms), corpus, no_client_reason
        )

    prompt = build_analysis_prompt(
        primary, hits, symptoms=symptoms, prior_findings=prior_findings
    )
    try:
        result = client.analyze_page(prompt)
    except QuotaExceededError:
        return _rule_based_page(
            ordered_runs, primary, list(symptoms), corpus, "quota_exhausted"
        )
    except MissingApiKeyError:
        return _rule_based_page(
            ordered_runs, primary, list(symptoms), corpus, "no_api_key"
        )
    except BudgetExhaustedError:
        # Ahead of the EmbeddingError clause on purpose: this subclasses it,
        # and "we chose not to spend" must not be reported as a bad response.
        return _rule_based_page(
            ordered_runs, primary, list(symptoms), corpus, "budget_exhausted"
        )
    except LlmUnavailableError:
        # A retired model or an unusable key is not a bad answer, and saying
        # "invalid_model_output" would send the reader to the wrong fix.
        return _rule_based_page(
            ordered_runs, primary, list(symptoms), corpus, "model_unavailable"
        )
    except (InvalidModelOutputError, AnalysisError, EmbeddingError):
        return _rule_based_page(
            ordered_runs, primary, list(symptoms), corpus, "invalid_model_output"
        )

    # -- citation validation: the model may cite only what it was shown ----- #
    allowed = {hit.source: hit.metadata for hit in hits if hit.source}
    kept = [r for r in result.recommendations if r.playbook_source in allowed]
    dropped = len(result.recommendations) - len(kept)

    if not kept:
        return _rule_based_page(
            ordered_runs, primary, list(symptoms), corpus,
            "no_grounded_recommendations", dropped=dropped,
        )

    metrics = _metrics_map(primary)
    candidates = [
        Candidate(source=rec.playbook_source, metadata=allowed[rec.playbook_source])
        for rec in kept
    ]
    projections = by_source(project(candidates, metrics))

    recommendations = build_recommendations(
        [
            (rec.title, rec.rationale, rec.playbook_source, rec.playbook_section,
             allowed[rec.playbook_source], getattr(rec, "why_it_matters", "") or "")
            for rec in kept
        ],
        projections,
    )

    detected = {s.code for s in symptoms}
    findings = [
        Finding(
            title=f.title,
            detail=f.detail,
            consequence=getattr(f, "consequence", "") or "",
            evidence=tuple(f.evidence),
            symptom_codes=tuple(c for c in f.symptom_codes if c in detected),
        )
        for f in result.findings
    ]

    flat = [p for rec in recommendations for p in rec.projections]
    return PageAnalysis(
        page_name=primary.page.name,
        page_url=primary.page.url,
        primary_run=primary,
        runs=ordered_runs,
        symptoms=list(symptoms),
        summary=result.summary,
        findings=findings,
        impacts=[Impact(audience=i.audience, text=i.text) for i in result.impacts],
        recommendations=recommendations,
        projections=aggregate(flat, metrics),
        mode="llm",
        degradation_reason=None,
        dropped_recommendations=dropped,
        playbooks_cited=sorted({r.playbook_source for r in recommendations}),
    )
