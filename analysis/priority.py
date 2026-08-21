"""One ordered plan across every page in the campaign.

The report used to present three per-page lists, and the executive summary took
its top three from whichever page sorted first alphabetically. On the live
Oakley campaign that put a 2041 ms homepage action above an 8636 ms blocking
time on the PDP: the reader's first question — what do I fix first — answered by
an accident of sort order.

Scoring is entirely rule-based, over projections ``analysis/estimator.py``
already computed. The model does not order this list, and that is deliberate:
ordering is a claim about magnitude, and magnitudes in this project come from
playbook metadata rather than from a model (§11).
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

#: How many actions the plan carries. A campaign of three pages produced
#: eighteen ranked recommendations, which is a backlog rather than a plan —
#: nobody reads to number fourteen. The rest are not lost: every page still
#: lists its own recommendations in full.
PLAN_LIMIT = 6

#: How much a page's own verdict on the metric weighs.
SEVERITY_WEIGHT = {"fail": 2.0, "warn": 1.0}
DEFAULT_WEIGHT = 0.5


def _severity_for(page: Any, metric: Optional[str]) -> float:
    """The weight of this page's worst symptom on the metric being improved."""
    if metric is None:
        return DEFAULT_WEIGHT
    weights = [
        SEVERITY_WEIGHT.get(symptom.severity, DEFAULT_WEIGHT)
        for symptom in page.symptoms
        if symptom.metric == metric
    ]
    return max(weights) if weights else DEFAULT_WEIGHT


def _gap_to_target(page: Any, metric: Optional[str], value: float,
                   thresholds: Any, glossary: Any) -> Optional[float]:
    """How far over target the metric is, or None when no target is set.

    The page's own ``targets`` win over the glossary's: they are what this
    campaign was judged against.
    """
    if metric is None:
        return None
    target = page.targets.get(metric)
    if target is None:
        target = glossary.target_for(metric, thresholds)
    return None if target is None else max(0.0, float(value) - float(target))


def score_action(page: Any, recommendation: Any, *, glossary: Any,
                 thresholds: Any) -> float:
    """Expected payoff of one recommendation, in metric units, severity-weighted.

    The gain is the *conservative* bound (``after_high``), and it is capped at
    the gap to target: shaving 2000 ms off a metric that is only 100 ms over
    buys 100 ms of value, not 2000.
    """
    if not recommendation.projections:
        return 0.0
    best = 0.0
    for projection in recommendation.projections:
        gain = max(0.0, float(projection.before) - float(projection.after_high))
        gap = _gap_to_target(page, projection.metric, float(projection.before),
                             thresholds, glossary)
        effective = min(gain, gap) if gap is not None else gain
        best = max(best, _severity_for(page, projection.metric) * effective)
    return best


def _primary_projection(recommendation: Any) -> Optional[Any]:
    """The projection with the largest conservative gain, for display."""
    if not recommendation.projections:
        return None
    return max(
        recommendation.projections,
        key=lambda p: (float(p.before) - float(p.after_high), p.metric),
    )


def rank_actions(pages: Sequence[Any], *, glossary: Any,
                 thresholds: Any) -> List[Any]:
    """Flatten every page's recommendations into one ranked plan."""
    from analysis.reportmodel import PlannedAction

    scored = []
    for page in pages:
        for recommendation in page.recommendations:
            scored.append((
                score_action(page, recommendation, glossary=glossary,
                             thresholds=thresholds),
                page,
                recommendation,
            ))

    # Descending by score, then page, then title: a total order, because two
    # runs over one campaign must render identical documents (§6.2).
    scored.sort(key=lambda row: (-row[0], row[1].name, row[2].title))

    plan: List[Any] = []
    for index, (_score, page, recommendation) in enumerate(scored[:PLAN_LIMIT],
                                                           start=1):
        projection = _primary_projection(recommendation)
        projected = ""
        metric = None
        if projection is not None:
            metric = projection.metric
            before = glossary.format_value(metric, projection.before)
            after = glossary.format_value(metric, projection.after_high)
            projected = f"{before} → {after}"
        plan.append(PlannedAction(
            rank=index,
            page=page.name,
            title=recommendation.title,
            why_it_matters=getattr(recommendation, "why_it_matters", "") or "",
            effort=recommendation.effort,
            metric=metric,
            projected=projected,
            playbook_source=recommendation.playbook_source,
        ))
    return plan
