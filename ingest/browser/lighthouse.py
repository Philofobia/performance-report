"""Programmatic Lighthouse audit over CDP (mockable).

``run_lighthouse`` is the integration point: it runs an audit against the page's
CDP session and maps the resulting Lighthouse report into our 0-100 category
scores. The heavy Node bridge is fully pluggable via a ``runner`` callable; the
default shell-out raises a clear error when the bridge isn't configured, so
offline unit tests always inject a fake runner (no real browser / no Node).

``category_scores`` is pure and unit-testable against canned Lighthouse reports.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

# our canonical LighthouseScores field name -> Lighthouse report category id.
CATEGORIES = {
    "performance": "performance",
    "accessibility": "accessibility",
    "best_practices": "best-practices",
    "seo": "seo",
}

LIGHTHOUSE_RUNNER: Optional[Callable[[str, object], dict]] = None


class LighthouseUnavailableError(RuntimeError):
    """Raised when a real Lighthouse audit is requested but the bridge is missing."""


def category_scores(lhr: dict) -> Dict[str, Optional[int]]:
    """Map a Lighthouse JSON report's category scores to 0-100 ints (None when absent).

    Lighthouse ``score`` values are floats in 0..1; we convert to 0-100 ints.
    """
    categories = lhr.get("categories") or {}
    scores: Dict[str, Optional[int]] = {}
    for our, lh in CATEGORIES.items():
        cat = categories.get(lh) or {}
        score = cat.get("score")
        scores[our] = None if score is None else int(round(score * 100))
    return scores


def _default_lighthouse_runner(url: str, cdp: object) -> dict:
    """Default bridge: requires the Node ``lighthouse`` CLI over a CDP endpoint.

    Automating a faithful Lighthouse audit needs the Node package wired to the
    page's CDP websocket. That bridge is out of scope for the offline CI; when a
    runner is not injected this raises with setup guidance.
    """
    raise LighthouseUnavailableError(
        "A real Lighthouse audit requires the Node `lighthouse` bridge over a CDP "
        "endpoint. Inject a `runner` callable(url, cdp) -> Lighthouse JSON report "
        "(e.g. ingest/browser/lighthouse.py's LIGHTHOUSE_RUNNER) in production."
    )


def run_lighthouse(
    url: str,
    cdp: object,
    *,
    runner: Optional[Callable[[str, object], dict]] = None,
) -> Dict[str, Optional[int]]:
    """Run a Lighthouse audit and return {category: 0-100} scores.

    ``runner`` defaults to :data:`LIGHTHOUSE_RUNNER`, then to the documented
    shell-out bridge. Tests pass a fake ``runner`` returning a canned report.
    """
    impl = runner or LIGHTHOUSE_RUNNER or _default_lighthouse_runner
    lhr = impl(url, cdp)
    return category_scores(lhr)
