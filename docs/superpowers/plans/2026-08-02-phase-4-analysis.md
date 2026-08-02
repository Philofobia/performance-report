# Phase 4 Analysis Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a campaign's stored runs plus their retrieved playbooks into a deterministic Report JSON containing findings, impacts, recommendations and rule-based improvement projections.

**Architecture:** Five modules under `analysis/`. A pure estimator supplies every number from playbook front matter; an LLM client supplies only prose and playbook *citations*, validated against what was actually retrieved. When no model is available the whole pipeline degrades to a rule-based path and still emits a schema-valid report. `reportmodel.py` assembles the JSON that Phase 5 will render.

**Tech Stack:** Python 3.11+, Pydantic 2.13, `google-genai` (lazy import, injected transport in tests), numpy (via existing store), pytest.

**Spec:** `docs/superpowers/specs/2026-08-02-phase-4-analysis-design.md` — read it before starting.

## Global Constraints

- **Determinism (SPEC §6.2):** every list in the Report JSON has an explicit total ordering. Identical runs produce byte-identical JSON except `cover.generated_at`.
- **No hallucinated magnitudes (SPEC §11):** no number in the report may originate from model output. `analysis/estimator.py` imports nothing from `analysis/llm.py` and never receives model prose.
- **Offline tests:** no test may require a network, a `GOOGLE_API_KEY`, or a browser. Every external call goes through an injected seam.
- **Test file naming:** `tests/unit/<topic>_test.py`, `tests/integration/<topic>_test.py` — this repo uses the `*_test.py` suffix, not the `test_*.py` prefix.
- **CLI convention:** `argparse`, a `_build_parser() -> argparse.ArgumentParser`, and `main(argv: Optional[List[str]] = None) -> int`, matching `ingest/automated.py`.
- **Coverage floor:** CI enforces ≥80%.
- **Secrets:** the API key is resolved only via `rag.embeddings.resolve_api_key()`; never logged, never placed in the Report JSON or an error message.
- **Type hints + `from __future__ import annotations`** at the top of every new module, matching the existing codebase.
- **Docstrings explain *why*,** matching the existing modules' voice — see `rag/prompt.py` for the register.

## File Structure

| File | Responsibility |
|---|---|
| `analysis/__init__.py` | Package marker; re-exports the error types |
| `analysis/estimator.py` | Pure projection math. No I/O, no imports from other `analysis` modules |
| `analysis/llm.py` | Google generation client, strict JSON contract, Pydantic output models |
| `analysis/findings.py` | Per-page orchestration: primary-run selection, LLM path, rule-based path, citation validation |
| `analysis/reportmodel.py` | Report JSON Pydantic models + assembly + deterministic ordering |
| `analysis/__main__.py` | `python -m analysis` CLI; loads runs, runs the pipeline, writes `report.json`, persists findings |
| `tests/unit/estimator_test.py` | Estimator math |
| `tests/unit/analysis_llm_test.py` | LLM client + JSON contract |
| `tests/unit/findings_test.py` | Primary selection, citation validation, rule-based path |
| `tests/unit/reportmodel_test.py` | Report assembly, ordering, verdict |
| `tests/integration/analysis_pipeline_test.py` | Determinism, degraded run, findings persistence, CLI |

**One clarification of the spec, applied throughout:** spec §7.1 orders recommendations by "projected absolute reduction descending". Absolute reductions are not comparable across metrics (600 ms vs 0.2 CLS), so the implementation orders by **`reduction_pct` descending**, then `playbook_source`, then `title`. Same intent, well-defined across metrics.

---

### Task 1: Estimator — parsing playbook impact ranges

**Files:**
- Create: `analysis/__init__.py`
- Create: `analysis/estimator.py`
- Test: `tests/unit/estimator_test.py`

**Interfaces:**
- Consumes: nothing (pure module, stdlib only)
- Produces:
  - `ImpactRange(metric: str, low: float, high: float, absolute: bool)` — frozen dataclass; `low`/`high` are fractions (0.15) for percentage ranges, metric-native units for absolute ones
  - `parse_impact_ranges(metadata: Mapping[str, Any]) -> List[ImpactRange]`
  - `effort_of(metadata: Mapping[str, Any]) -> str`
  - `Candidate(source: str, metadata: Mapping[str, Any])` — frozen dataclass, the estimator's view of a recommendation

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/estimator_test.py`:

```python
"""Unit tests for analysis/estimator.py — the rule-based projection math.

The estimator is pure: no store, no client, no config. Every number in the
report comes from here, so these tests are the guard against SPEC §11's
"LLM hallucinated improvement magnitudes" risk.
"""
from __future__ import annotations

import pytest

from analysis.estimator import (
    Candidate,
    ImpactRange,
    effort_of,
    parse_impact_ranges,
)


def test_parses_percentage_range_into_fractions():
    ranges = parse_impact_ranges({"expected_lcp_reduction_pct": [15, 40]})
    assert ranges == [ImpactRange(metric="lcp_ms", low=0.15, high=0.40, absolute=False)]


def test_parses_absolute_range_for_cls():
    ranges = parse_impact_ranges({"expected_cls_reduction_abs": [0.05, 0.15]})
    assert ranges == [ImpactRange(metric="cls", low=0.05, high=0.15, absolute=True)]


def test_single_scalar_becomes_a_degenerate_range():
    ranges = parse_impact_ranges({"expected_ttfb_reduction_pct": 30})
    assert ranges == [ImpactRange(metric="ttfb_ms", low=0.30, high=0.30, absolute=False)]


def test_ranges_are_sorted_by_metric_for_determinism():
    ranges = parse_impact_ranges({
        "expected_ttfb_reduction_pct": [30, 80],
        "expected_lcp_reduction_pct": [15, 40],
    })
    assert [r.metric for r in ranges] == ["lcp_ms", "ttfb_ms"]


def test_unknown_and_malformed_keys_are_ignored():
    meta = {
        "expected_bogus_reduction_pct": [10, 20],   # unknown metric stem
        "expected_lcp_reduction_pct": "not a number",
        "category": "images",
    }
    assert parse_impact_ranges(meta) == []


def test_reversed_bounds_are_normalised():
    ranges = parse_impact_ranges({"expected_lcp_reduction_pct": [40, 15]})
    assert ranges == [ImpactRange(metric="lcp_ms", low=0.15, high=0.40, absolute=False)]


def test_effort_defaults_to_unknown():
    assert effort_of({"effort": "low"}) == "low"
    assert effort_of({"effort": "LOW"}) == "low"
    assert effort_of({}) == "unknown"
    assert effort_of({"effort": "wildly speculative"}) == "unknown"


def test_candidate_is_hashable_and_carries_its_source():
    c = Candidate(source="images.md", metadata={"effort": "low"})
    assert c.source == "images.md"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/estimator_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analysis'`

- [ ] **Step 3: Create the package marker**

Create `analysis/__init__.py`:

```python
"""Analysis layer: runs + retrieved playbooks -> deterministic Report JSON.

See docs/superpowers/specs/2026-08-02-phase-4-analysis-design.md.
"""
```

- [ ] **Step 4: Write the estimator's parsing half**

Create `analysis/estimator.py`:

```python
"""Rule-based improvement projections (PROJECT_SPEC §6 section 6, §11).

This module is deliberately **pure**: no store, no config, no LLM, no file
I/O. That is not tidiness — it is the mitigation for §11's "LLM hallucinated
improvement magnitudes" risk. The model picks *which* playbook applies; the
numbers come from here, and this code cannot see a single word the model
wrote.

Magnitudes originate in playbook front matter, which
``rag/knowledge.py`` already parses and carries through
``Chunk.metadata -> Document.metadata -> SearchHit.metadata``:

    expected_lcp_reduction_pct: 15, 40
    expected_cls_reduction_abs: 0.05, 0.15
    effort: low

Percentages are stored as fractions internally so the arithmetic never has to
remember which unit it is in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence

# How much a second, third, ... fix on the *same* metric is discounted. Stacked
# optimisations overlap: the second image fix cannot re-win bytes the first one
# already removed.
DECAY = 0.8

# No stack of recommendations may claim more than this share of a metric.
MAX_TOTAL_REDUCTION = 0.70

_VALID_EFFORTS = ("low", "medium", "high")

# Front-matter stems -> canonical metric names on the run object.
_METRIC_ALIASES = {
    "lcp": "lcp_ms",
    "fcp": "fcp_ms",
    "ttfb": "ttfb_ms",
    "inp": "inp_ms",
    "tbt": "tbt_ms",
    "cls": "cls",
    "transfer": "total_transfer_kb",
    "page_weight": "total_transfer_kb",
}

_RANGE_KEY = re.compile(r"^expected_(?P<stem>[a-z_]+?)_reduction_(?P<unit>pct|abs)$")


@dataclass(frozen=True)
class ImpactRange:
    """One playbook's expected effect on one metric.

    ``low``/``high`` are fractions (0.15 == 15%) when ``absolute`` is False,
    and metric-native units (0.05 CLS) when it is True.
    """

    metric: str
    low: float
    high: float
    absolute: bool = False


@dataclass(frozen=True)
class Candidate:
    """A recommendation as the estimator sees it: a source and its metadata.

    Deliberately not the richer ``Recommendation`` from ``findings.py`` — the
    estimator must not depend on anything that has touched model output.
    """

    source: str
    metadata: Mapping[str, Any]


def _as_bounds(value: Any) -> Optional[tuple]:
    """Coerce a front-matter value into an ordered ``(low, high)`` pair.

    ``rag.knowledge._coerce`` turns "15, 40" into ``[15, 40]`` and a bare "30"
    into ``30``, so both shapes arrive here.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (float(value), float(value))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            low, high = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
        return (min(low, high), max(low, high))
    return None


def parse_impact_ranges(metadata: Mapping[str, Any]) -> List[ImpactRange]:
    """Read every ``expected_<metric>_reduction_<pct|abs>`` key.

    Sorted by metric so a playbook always yields its ranges in the same order —
    the first link in the determinism chain (§6.2).
    """
    found: List[ImpactRange] = []
    for key, raw in metadata.items():
        match = _RANGE_KEY.match(str(key))
        if not match:
            continue
        metric = _METRIC_ALIASES.get(match.group("stem"))
        if metric is None:
            continue
        bounds = _as_bounds(raw)
        if bounds is None:
            continue
        low, high = bounds
        absolute = match.group("unit") == "abs"
        if not absolute:
            low, high = low / 100.0, high / 100.0
        found.append(ImpactRange(metric=metric, low=low, high=high, absolute=absolute))
    return sorted(found, key=lambda r: r.metric)


def effort_of(metadata: Mapping[str, Any]) -> str:
    """Playbook effort level, or ``"unknown"`` when absent or unrecognised."""
    value = str(metadata.get("effort", "")).strip().lower()
    return value if value in _VALID_EFFORTS else "unknown"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit/estimator_test.py -v`
Expected: PASS, 8 tests

- [ ] **Step 6: Commit**

```bash
git add analysis/__init__.py analysis/estimator.py tests/unit/estimator_test.py
git commit -m "Add estimator front-matter parsing for impact ranges"
```

---

### Task 2: Estimator — stacking, decay and aggregation

**Files:**
- Modify: `analysis/estimator.py`
- Test: `tests/unit/estimator_test.py`

**Interfaces:**
- Consumes: `ImpactRange`, `Candidate`, `parse_impact_ranges`, `DECAY`, `MAX_TOTAL_REDUCTION` from Task 1
- Produces:
  - `Projection(metric: str, before: float, after_low: float, after_high: float, reduction_pct: float, source: str)` — frozen dataclass
  - `project(candidates: Sequence[Candidate], metrics: Mapping[str, Optional[float]]) -> List[Projection]`
  - `aggregate(projections: Sequence[Projection], metrics: Mapping[str, Optional[float]]) -> Dict[str, Projection]`
  - `by_source(projections: Sequence[Projection]) -> Dict[str, List[Projection]]`
  - `rank_key(source: str, title: str, projections: Sequence[Projection]) -> tuple` — the deterministic recommendation sort key

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/estimator_test.py`:

```python
from analysis.estimator import (
    DECAY,
    MAX_TOTAL_REDUCTION,
    Projection,
    aggregate,
    by_source,
    project,
    rank_key,
)

METRICS = {"lcp_ms": 6200.0, "cls": 0.42, "ttfb_ms": 1800.0, "inp_ms": None}


def _cand(source, **meta):
    return Candidate(source=source, metadata=meta)


def test_single_candidate_uses_the_low_bound_for_the_headline():
    out = project([_cand("images.md", expected_lcp_reduction_pct=[15, 40])], METRICS)
    assert len(out) == 1
    p = out[0]
    assert p.metric == "lcp_ms"
    assert p.before == 6200.0
    assert p.after_low == pytest.approx(6200 * 0.85)     # conservative
    assert p.after_high == pytest.approx(6200 * 0.60)    # optimistic band edge
    assert p.reduction_pct == pytest.approx(0.15)
    assert p.source == "images.md"


def test_second_fix_on_the_same_metric_is_decayed():
    out = project(
        [
            _cand("images.md", expected_lcp_reduction_pct=[20, 40]),
            _cand("fonts.md", expected_lcp_reduction_pct=[10, 30]),
        ],
        METRICS,
    )
    first, second = out[0], out[1]
    assert first.source == "images.md"          # larger low bound applies first
    assert first.after_low == pytest.approx(6200 * 0.80)
    # second candidate's 10% is discounted by DECAY and applied to the remainder
    expected = first.after_low * (1 - 0.10 * DECAY)
    assert second.before == pytest.approx(first.after_low)
    assert second.after_low == pytest.approx(expected)


def test_cumulative_reduction_is_capped():
    heavy = [
        _cand(f"p{i}.md", expected_lcp_reduction_pct=[50, 60]) for i in range(6)
    ]
    out = project(heavy, METRICS)
    floor = 6200.0 * (1 - MAX_TOTAL_REDUCTION)
    assert min(p.after_low for p in out) >= floor - 1e-6
    assert out[-1].after_low == pytest.approx(floor)


def test_absolute_ranges_subtract_instead_of_scaling():
    out = project([_cand("fonts.md", expected_cls_reduction_abs=[0.05, 0.15])], METRICS)
    p = out[0]
    assert p.metric == "cls"
    assert p.after_low == pytest.approx(0.42 - 0.05)
    assert p.after_high == pytest.approx(0.42 - 0.15)


def test_projection_never_goes_negative():
    out = project([_cand("x.md", expected_cls_reduction_abs=[9.0, 9.0])], {"cls": 0.42})
    assert out[0].after_low == 0.0


def test_candidate_with_no_range_yields_nothing():
    assert project([_cand("prose-only.md", effort="low")], METRICS) == []


def test_range_for_an_unmeasured_metric_is_skipped():
    # inp_ms is None in METRICS - nothing to project from.
    assert project([_cand("js.md", expected_inp_reduction_pct=[20, 40])], METRICS) == []


def test_ordering_is_stable_for_equal_bounds():
    out = project(
        [
            _cand("zebra.md", expected_lcp_reduction_pct=[20, 40]),
            _cand("alpha.md", expected_lcp_reduction_pct=[20, 40]),
        ],
        METRICS,
    )
    assert [p.source for p in out] == ["alpha.md", "zebra.md"]


def test_aggregate_reports_first_before_and_last_after_per_metric():
    out = project(
        [
            _cand("images.md", expected_lcp_reduction_pct=[20, 40]),
            _cand("fonts.md", expected_lcp_reduction_pct=[10, 30]),
            _cand("caching.md", expected_ttfb_reduction_pct=[30, 80]),
        ],
        METRICS,
    )
    agg = aggregate(out, METRICS)
    assert set(agg) == {"lcp_ms", "ttfb_ms"}
    assert agg["lcp_ms"].before == 6200.0
    assert agg["lcp_ms"].after_low == pytest.approx(out[1].after_low)
    assert agg["lcp_ms"].source == "aggregate"
    assert agg["ttfb_ms"].reduction_pct == pytest.approx(0.30)


def test_aggregate_of_nothing_is_empty():
    assert aggregate([], METRICS) == {}


def test_by_source_groups_projections():
    out = project(
        [
            _cand("images.md", expected_lcp_reduction_pct=[20, 40]),
            _cand("caching.md", expected_ttfb_reduction_pct=[30, 80]),
        ],
        METRICS,
    )
    grouped = by_source(out)
    assert set(grouped) == {"images.md", "caching.md"}
    assert grouped["images.md"][0].metric == "lcp_ms"


def test_rank_key_orders_by_reduction_then_source_then_title():
    big = [Projection("lcp_ms", 6200, 5000, 4000, 0.20, "a.md")]
    small = [Projection("cls", 0.42, 0.40, 0.30, 0.05, "b.md")]
    assert rank_key("a.md", "Big win", big) < rank_key("b.md", "Small win", small)
    # no projections at all sorts last, deterministically
    assert rank_key("z.md", "Unknown", []) > rank_key("b.md", "Small win", small)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/estimator_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'Projection' from 'analysis.estimator'`

- [ ] **Step 3: Implement the math**

Append to `analysis/estimator.py`:

```python
@dataclass(frozen=True)
class Projection:
    """One recommendation's projected effect on one metric.

    ``after_low`` is the conservative end and drives the headline and the
    chart; ``after_high`` is the optimistic edge of the playbook's own band.
    Under-promising and landing beats a midpoint that misses.
    """

    metric: str
    before: float
    after_low: float
    after_high: float
    reduction_pct: float
    source: str


def _apply(value: float, amount: float, absolute: bool, floor: float) -> float:
    """Apply one reduction to ``value``, clamped at ``floor`` and at zero."""
    reduced = value - amount if absolute else value * (1.0 - amount)
    return max(0.0, floor, reduced)


def project(
    candidates: Sequence[Candidate],
    metrics: Mapping[str, Optional[float]],
) -> List[Projection]:
    """Project each candidate's effect, stacking per metric with decay.

    Candidates affecting the same metric are applied in descending order of
    their low bound (ties broken by source, so the order never depends on how
    the caller happened to sort them). Each subsequent fix on that metric is
    discounted by ``DECAY`` and applied to what the previous one left.

    A candidate with no usable range for any *measured* metric contributes
    nothing — the caller still lists it, with magnitude "unknown", per the
    system prompt's rule 3.
    """
    # Bucket (candidate, range) pairs by metric, keeping only measured metrics.
    buckets: dict = {}
    for candidate in candidates:
        for rng in parse_impact_ranges(candidate.metadata):
            measured = metrics.get(rng.metric)
            if measured is None:
                continue
            buckets.setdefault(rng.metric, []).append((candidate.source, rng))

    projections: List[Projection] = []
    for metric in sorted(buckets):
        baseline = float(metrics[metric])
        floor = baseline * (1.0 - MAX_TOTAL_REDUCTION)
        entries = sorted(buckets[metric], key=lambda pair: (-pair[1].low, pair[0]))

        current_low = baseline
        current_high = baseline
        for position, (source, rng) in enumerate(entries):
            decay = DECAY ** position
            before = current_low
            after_low = _apply(current_low, rng.low * decay, rng.absolute, floor)
            after_high = _apply(current_high, rng.high * decay, rng.absolute, floor)
            reduction = 0.0 if before <= 0 else (before - after_low) / before
            projections.append(
                Projection(
                    metric=metric,
                    before=before,
                    after_low=after_low,
                    after_high=after_high,
                    reduction_pct=reduction,
                    source=source,
                )
            )
            current_low, current_high = after_low, after_high

    return projections


def aggregate(
    projections: Sequence[Projection],
    metrics: Mapping[str, Optional[float]],
) -> dict:
    """Collapse per-metric chains into one before/after each (§6 chart)."""
    out: dict = {}
    for metric in sorted({p.metric for p in projections}):
        chain = [p for p in projections if p.metric == metric]
        baseline = float(metrics[metric])
        last = chain[-1]
        reduction = 0.0 if baseline <= 0 else (baseline - last.after_low) / baseline
        out[metric] = Projection(
            metric=metric,
            before=baseline,
            after_low=last.after_low,
            after_high=last.after_high,
            reduction_pct=reduction,
            source="aggregate",
        )
    return out


def by_source(projections: Sequence[Projection]) -> dict:
    """Group projections by the playbook that produced them."""
    out: dict = {}
    for projection in projections:
        out.setdefault(projection.source, []).append(projection)
    return out


def rank_key(source: str, title: str, projections: Sequence[Projection]) -> tuple:
    """Deterministic sort key for recommendations (§7.1).

    Percentage, not absolute delta: 600ms and 0.2 CLS are not comparable, but
    "cuts this metric by 20%" is. Recommendations with no projection sort last
    rather than being dropped.
    """
    best = max((p.reduction_pct for p in projections), default=0.0)
    return (-best, source, title)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/estimator_test.py -v`
Expected: PASS, 20 tests

- [ ] **Step 5: Verify the purity constraint holds**

Run: `grep -nE "^(from|import) " analysis/estimator.py`
Expected: only `__future__`, `re`, `dataclasses`, `typing`. No `analysis.llm`, no `rag`, no `store`, no `config`.

- [ ] **Step 6: Commit**

```bash
git add analysis/estimator.py tests/unit/estimator_test.py
git commit -m "Add estimator stacking, decay cap and aggregation"
```

---

### Task 3: LLM client and the strict JSON contract

**Files:**
- Create: `analysis/llm.py`
- Test: `tests/unit/analysis_llm_test.py`

**Interfaces:**
- Consumes: `rag.embeddings.resolve_api_key`, `rag.embeddings.backoff_delays`, `rag.embeddings._is_quota_error`, `rag.embeddings.QuotaExceededError`, `rag.prompt.GroundedPrompt`
- Produces:
  - `AnalysisError`, `LlmUnavailableError`, `InvalidModelOutputError`
  - `LlmFinding`, `LlmImpact`, `LlmRecommendation`, `LlmPageAnalysis`, `LlmSummary` (Pydantic)
  - `extract_json(text: str) -> str`
  - `GoogleAnalysisClient(model=..., api_key=None, transport=None, max_retries=3, sleep=time.sleep, jitter=random.random)` with `.model`, `.analyze_page(prompt: GroundedPrompt) -> LlmPageAnalysis`, `.summarize(payload: str) -> LlmSummary`
  - Transport seam: `(messages: List[Dict[str, str]], model: str) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/analysis_llm_test.py`:

```python
"""Unit tests for analysis/llm.py.

The transport is always a fake returning canned strings: no google-genai, no
key, no network (TESTING_PLAN.md §3).
"""
from __future__ import annotations

import json

import pytest

from analysis.llm import (
    GoogleAnalysisClient,
    InvalidModelOutputError,
    LlmPageAnalysis,
    LlmSummary,
    extract_json,
)
from rag.embeddings import QuotaExceededError
from rag.prompt import GroundedPrompt

VALID_PAGE = {
    "summary": "The hero video dominates the LCP path.",
    "findings": [
        {
            "title": "Hero video is the LCP element",
            "detail": "It transfers 2140KB before first paint.",
            "evidence": ["lcp_ms=6200", "hero.mp4 2140KB"],
            "symptom_codes": ["lcp_fail"],
        }
    ],
    "impacts": [{"audience": "ux", "text": "Users stare at an empty hero."}],
    "recommendations": [
        {
            "title": "Replace the autoplay video with a poster image",
            "rationale": "A poster removes 2MB from the critical path.",
            "playbook_source": "images.md",
            "playbook_section": "Serve modern formats",
        }
    ],
}


class FakeTransport:
    """Returns canned responses in order; records what it was asked."""

    def __init__(self, responses, error=None, fail_times=0):
        self._responses = list(responses)
        self._error = error
        self._fail_times = fail_times
        self.calls = []

    def __call__(self, messages, model):
        self.calls.append({"messages": messages, "model": model})
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._error
        return self._responses.pop(0)


def make_client(responses, **kwargs):
    kwargs.setdefault("transport", FakeTransport(responses, **{
        k: kwargs.pop(k) for k in ("error", "fail_times") if k in kwargs
    }))
    kwargs.setdefault("sleep", lambda s: None)
    kwargs.setdefault("jitter", lambda: 1.0)
    return GoogleAnalysisClient(model="test-llm", **kwargs)


def a_prompt():
    return GroundedPrompt(system="SYSTEM", user="USER", sources=["images.md"])


# -- extract_json ----------------------------------------------------------- #
def test_extract_json_passes_through_bare_object():
    assert json.loads(extract_json('{"a": 1}')) == {"a": 1}


def test_extract_json_unwraps_a_fenced_block():
    raw = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
    assert json.loads(extract_json(raw)) == {"a": 1}


def test_extract_json_handles_nested_braces_and_strings():
    raw = 'prefix {"a": {"b": "}"}, "c": 2} suffix'
    assert json.loads(extract_json(raw)) == {"a": {"b": "}"}, "c": 2}


def test_extract_json_raises_when_there_is_no_object():
    with pytest.raises(InvalidModelOutputError):
        extract_json("I would rather not.")


# -- analyze_page ----------------------------------------------------------- #
def test_valid_json_is_parsed_into_the_model():
    client = make_client([json.dumps(VALID_PAGE)])
    result = client.analyze_page(a_prompt())
    assert isinstance(result, LlmPageAnalysis)
    assert result.recommendations[0].playbook_source == "images.md"
    assert result.findings[0].symptom_codes == ["lcp_fail"]


def test_prompt_messages_reach_the_transport_unaltered_in_role_order():
    transport = FakeTransport([json.dumps(VALID_PAGE)])
    client = GoogleAnalysisClient(model="test-llm", transport=transport,
                                  sleep=lambda s: None, jitter=lambda: 1.0)
    client.analyze_page(a_prompt())
    messages = transport.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "SYSTEM"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("USER")


def test_malformed_output_is_retried_once_then_succeeds():
    client = make_client(["not json at all", json.dumps(VALID_PAGE)])
    result = client.analyze_page(a_prompt())
    assert result.summary == VALID_PAGE["summary"]


def test_the_retry_turn_tells_the_model_what_broke():
    transport = FakeTransport(["not json at all", json.dumps(VALID_PAGE)])
    client = GoogleAnalysisClient(model="test-llm", transport=transport,
                                  sleep=lambda s: None, jitter=lambda: 1.0)
    client.analyze_page(a_prompt())
    retry_user = transport.calls[1]["messages"][-1]["content"]
    assert "valid JSON" in retry_user


def test_malformed_twice_raises():
    client = make_client(["nope", "still nope"])
    with pytest.raises(InvalidModelOutputError):
        client.analyze_page(a_prompt())


def test_schema_violation_counts_as_malformed():
    bad = {"summary": "x", "findings": [{"title": "t"}],
           "impacts": [{"audience": "marketing", "text": "t"}],
           "recommendations": []}
    client = make_client([json.dumps(bad), json.dumps(bad)])
    with pytest.raises(InvalidModelOutputError):
        client.analyze_page(a_prompt())


def test_quota_error_retries_with_backoff_then_raises():
    transport = FakeTransport([], error=RuntimeError("429 RESOURCE_EXHAUSTED"),
                              fail_times=99)
    slept = []
    client = GoogleAnalysisClient(model="test-llm", transport=transport,
                                  max_retries=3, sleep=slept.append,
                                  jitter=lambda: 1.0)
    with pytest.raises(QuotaExceededError):
        client.analyze_page(a_prompt())
    assert slept == [1.0, 2.0, 4.0]


def test_non_quota_transport_errors_propagate_unwrapped():
    transport = FakeTransport([], error=ValueError("bad argument"), fail_times=1)
    client = GoogleAnalysisClient(model="test-llm", transport=transport,
                                  sleep=lambda s: None, jitter=lambda: 1.0)
    with pytest.raises(ValueError):
        client.analyze_page(a_prompt())


# -- summarize -------------------------------------------------------------- #
def test_summarize_parses_and_truncates_top_actions():
    payload = {"problem": "p", "key_finding": "k",
               "top_actions": ["a", "b", "c", "d"]}
    client = make_client([json.dumps(payload)])
    result = client.summarize("per-page findings here")
    assert isinstance(result, LlmSummary)
    assert result.top_actions == ["a", "b", "c"]


def test_summarize_rejects_an_empty_action_list():
    payload = {"problem": "p", "key_finding": "k", "top_actions": []}
    client = make_client([json.dumps(payload), json.dumps(payload)])
    with pytest.raises(InvalidModelOutputError):
        client.summarize("findings")


def test_no_api_key_and_no_transport_is_reported_clearly(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = GoogleAnalysisClient(model="test-llm")
    from rag.embeddings import MissingApiKeyError
    with pytest.raises(MissingApiKeyError):
        client.analyze_page(a_prompt())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/analysis_llm_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analysis.llm'`

- [ ] **Step 3: Implement the client**

Create `analysis/llm.py`:

```python
"""Google generation client with a strict JSON output contract.

The model's job in this system is narrow: write prose, and say *which*
playbook justifies each recommendation. It is given no field in which to put
a number — cheaper than deleting hallucinated magnitudes after the fact, and
the reason ``estimator.py`` can stay the sole source of every figure in the
report (§11).

Everything else mirrors ``rag/embeddings.py`` so the codebase has one shape
for "calls Google": injected transport, lazy SDK import, quota backoff with
jitter, typed errors, and a key that is resolved in exactly one place and
never logged.

One retry on malformed output, with the parse error fed back as a correction
turn. Models usually fix their own JSON on the second attempt; a third try
mostly buys latency. After that the caller degrades to the rule-based path.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator
from typing_extensions import Literal

from rag.embeddings import (
    QuotaExceededError,
    _is_quota_error,
    backoff_delays,
    resolve_api_key,
)
from rag.prompt import GroundedPrompt

DEFAULT_MAX_RETRIES = 3

JSON_INSTRUCTION = """

# OUTPUT FORMAT
Reply with a single JSON object and nothing else - no prose before or after,
no code fence. Shape:

{
  "summary": "one paragraph on what is wrong with this page",
  "findings": [
    {"title": "...", "detail": "...", "evidence": ["metric=value"],
     "symptom_codes": ["lcp_fail"]}
  ],
  "impacts": [{"audience": "ux|seo|business", "text": "..."}],
  "recommendations": [
    {"title": "...", "rationale": "...",
     "playbook_source": "<the source name of a playbook shown above>",
     "playbook_section": "<the heading you used>"}
  ]
}

Do not include numeric estimates of improvement anywhere. Magnitudes are
computed from the playbooks by this system, not by you.\
"""

SUMMARY_SYSTEM = """\
You are a web performance analyst writing the executive summary of a report.
You are given findings this system already produced. Synthesise them. Do not
introduce new claims, new metrics, or numeric improvement estimates.

Reply with a single JSON object and nothing else:

{"problem": "...", "key_finding": "...", "top_actions": ["...", "...", "..."]}\
"""


class AnalysisError(Exception):
    """Base class for analysis failures."""


class LlmUnavailableError(AnalysisError):
    """No usable model client (SDK missing, or generation unreachable)."""


class InvalidModelOutputError(AnalysisError):
    """The model did not return output matching the required JSON contract."""


# --------------------------------------------------------------------------- #
# Output models — the contract. No numeric fields, by design.
# --------------------------------------------------------------------------- #
class LlmFinding(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=2000)
    evidence: List[str] = Field(default_factory=list, max_length=20)
    symptom_codes: List[str] = Field(default_factory=list, max_length=20)


class LlmImpact(BaseModel):
    audience: Literal["ux", "seo", "business"]
    text: str = Field(min_length=1, max_length=1000)


class LlmRecommendation(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(default="", max_length=2000)
    playbook_source: str = Field(min_length=1, max_length=200)
    playbook_section: str = Field(default="", max_length=200)


class LlmPageAnalysis(BaseModel):
    summary: str = Field(default="", max_length=4000)
    findings: List[LlmFinding] = Field(default_factory=list, max_length=20)
    impacts: List[LlmImpact] = Field(default_factory=list, max_length=20)
    recommendations: List[LlmRecommendation] = Field(default_factory=list, max_length=20)


class LlmSummary(BaseModel):
    problem: str = Field(min_length=1, max_length=2000)
    key_finding: str = Field(min_length=1, max_length=2000)
    top_actions: List[str] = Field(min_length=1, max_length=3)

    @field_validator("top_actions", mode="before")
    @classmethod
    def _truncate(cls, value: Any) -> Any:
        """Take the first three rather than rejecting an over-long list.

        A model that offers five good actions has not failed the contract; it
        has been generous. Truncating is the graceful read.
        """
        return value[:3] if isinstance(value, list) else value


def extract_json(text: str) -> str:
    """Pull the outermost balanced JSON object out of a model response.

    Models wrap JSON in fences and apologies no matter how firmly the prompt
    says not to. Brace-matching is string-aware so a ``}`` inside a value does
    not end the object early.
    """
    start = text.find("{")
    if start == -1:
        raise InvalidModelOutputError(
            "Model response contained no JSON object."
        )
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise InvalidModelOutputError("Model response had an unterminated JSON object.")


class GoogleAnalysisClient:
    """Google generation with retry/backoff and a validated JSON contract.

    ``transport`` is the injection point: ``(messages, model) -> str``. When
    omitted a real ``google-genai`` client is built lazily, so importing this
    module never requires the SDK or a key.
    """

    def __init__(
        self,
        *,
        model: str = "gemini-2.0-flash",
        api_key: Optional[str] = None,
        transport: Optional[Callable[[List[Dict[str, str]], str], str]] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.model = model
        self._explicit_key = api_key
        self._transport = transport
        self._max_retries = max(0, max_retries)
        self._sleep = sleep
        self._jitter = jitter

    # -- transport --------------------------------------------------------- #
    def _build_default_transport(self):
        key = resolve_api_key(self._explicit_key)
        try:
            from google import genai  # lazy: tests never need the SDK
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise LlmUnavailableError(
                "google-genai is not installed. Install it with "
                "`pip install -r requirements.txt` to use live analysis."
            ) from exc

        client = genai.Client(api_key=key)

        def transport(messages, model):
            system = "\n".join(
                m["content"] for m in messages if m["role"] == "system"
            )
            contents = [m["content"] for m in messages if m["role"] != "system"]
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config={
                    "system_instruction": system,
                    "temperature": 0,
                    "response_mime_type": "application/json",
                },
            )
            return response.text or ""

        return transport

    def _call(self, messages: List[Dict[str, str]]) -> str:
        """One generation call, retried on quota errors only."""
        if self._transport is None:
            self._transport = self._build_default_transport()

        delays = backoff_delays(self._max_retries, jitter=self._jitter)
        last_exc: Optional[BaseException] = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._transport(messages, self.model)
            except Exception as exc:
                if not _is_quota_error(exc):
                    raise
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                self._sleep(delays[attempt])

        raise QuotaExceededError(
            f"Google AI quota exhausted after {self._max_retries} retries. "
            "The free tier limits requests per minute; wait for the window to "
            "reset or re-run with --no-llm."
        ) from last_exc

    # -- validated generation ---------------------------------------------- #
    def _generate_validated(self, messages: List[Dict[str, str]], model_cls):
        """Call, parse and validate, with one corrective retry."""
        attempt_messages = list(messages)
        problem: Optional[str] = None
        for attempt in range(2):
            raw = self._call(attempt_messages)
            try:
                return model_cls.model_validate(json.loads(extract_json(raw)))
            except (InvalidModelOutputError, json.JSONDecodeError, ValidationError) as exc:
                problem = str(exc)[:500]
                if attempt == 1:
                    break
                attempt_messages = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply was not valid JSON matching the "
                            f"required shape. The error was: {problem}\n"
                            "Reply again with only the JSON object."
                        ),
                    }
                ]
        raise InvalidModelOutputError(
            f"Model did not return valid JSON after 2 attempts: {problem}"
        )

    def analyze_page(self, prompt: GroundedPrompt) -> LlmPageAnalysis:
        """Analyse one page from its grounded prompt."""
        messages = prompt.as_messages()
        messages[-1] = {
            "role": messages[-1]["role"],
            "content": messages[-1]["content"] + JSON_INSTRUCTION,
        }
        return self._generate_validated(messages, LlmPageAnalysis)

    def summarize(self, payload: str) -> LlmSummary:
        """Write the executive summary from already-produced findings."""
        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": payload},
        ]
        return self._generate_validated(messages, LlmSummary)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/analysis_llm_test.py -v`
Expected: PASS, 14 tests

If `typing_extensions` is not installed, change the import to `from typing import Literal` — Python 3.11+ has it in the stdlib. Verify with `python -c "from typing import Literal"` and prefer the stdlib import.

- [ ] **Step 5: Commit**

```bash
git add analysis/llm.py tests/unit/analysis_llm_test.py
git commit -m "Add LLM client with strict JSON contract and quota backoff"
```

---

### Task 4: Findings — primary-run selection and the rule-based path

**Files:**
- Create: `analysis/findings.py`
- Test: `tests/unit/findings_test.py`

**Interfaces:**
- Consumes: `normalize.schema.Run`, `rag.retrieve.Symptom`/`detect_symptoms`, `rag.knowledge.load_knowledge_dir`/`Chunk`, `analysis.estimator.*`
- Produces:
  - `Finding(title, detail, evidence, symptom_codes)`, `Impact(audience, text)`, `Recommendation(title, rationale, playbook_source, playbook_section, effort, projections)` — frozen dataclasses
  - `PageAnalysis` dataclass (fields listed in Step 3)
  - `select_primary(runs: Sequence[Run]) -> Run`
  - `match_playbooks_by_symptoms(symptom_codes, chunks) -> List[Chunk]`
  - `rule_based_analysis(run, symptoms, chunks) -> tuple[str, List[Finding], List[Impact], List[Candidate-ish]]` — exact return type in Step 3

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/findings_test.py`:

```python
"""Unit tests for analysis/findings.py — selection and the rule-based path.

No store, no client, no key: the rule-based path must work with nothing but
the runs and the playbooks on disk.
"""
from __future__ import annotations

import pytest

from analysis.findings import (
    Finding,
    Impact,
    Recommendation,
    match_playbooks_by_symptoms,
    rule_based_analysis,
    select_primary,
)
from config.load import Thresholds
from normalize.schema import Run
from rag import knowledge, retrieve


def make_run(run_id="run_a", lcp=6200, cls=0.42, inp=480, page="homepage",
             device="mid-mobile", network="slow-4g"):
    return Run.model_validate({
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com"},
        "page": {"name": page, "url": "https://example.com/"},
        "condition": {"device": device, "network": network,
                      "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": "2026-01-08T14:30:00Z", "source": "automated"},
        "metrics": {
            "cwp": {"lcp_ms": lcp, "cls": cls, "inp_ms": inp, "fcp_ms": 3100,
                    "ttfb_ms": 1800, "tbt_ms": 620},
            "network": {"total_transfer_kb": 4820, "request_count": 118,
                        "render_blocking_css": 6},
            "main_thread": {"script_ms": 1820, "task_ms": 3100, "dom_nodes": 3200},
        },
        "resource_timings": [
            {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140,
             "duration_ms": 390},
        ],
    })


# -- select_primary --------------------------------------------------------- #
def test_primary_is_the_run_with_most_failing_symptoms():
    healthy = make_run("run_ok", lcp=1800, cls=0.02, inp=90)
    broken = make_run("run_bad", lcp=6200, cls=0.42, inp=480)
    assert select_primary([healthy, broken]).run_id == "run_bad"


def test_primary_breaks_ties_on_lcp_then_run_id():
    a = make_run("run_b", lcp=5000, cls=0.42, inp=480)
    b = make_run("run_a", lcp=5000, cls=0.42, inp=480)
    assert select_primary([a, b]).run_id == "run_a"


def test_primary_selection_is_order_independent():
    a = make_run("run_a", lcp=3000, cls=0.05, inp=100)
    b = make_run("run_b", lcp=6200, cls=0.42, inp=480)
    assert select_primary([a, b]).run_id == select_primary([b, a]).run_id


def test_primary_of_empty_raises():
    with pytest.raises(ValueError):
        select_primary([])


# -- playbook matching ------------------------------------------------------ #
def test_matching_selects_playbooks_whose_front_matter_lists_the_symptom():
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    matched = match_playbooks_by_symptoms(["lcp_fail"], chunks)
    assert matched
    assert all("lcp_fail" in _symptoms_of(c) for c in matched)


def _symptoms_of(chunk):
    raw = chunk.metadata.get("symptoms", [])
    return raw if isinstance(raw, list) else [raw]


def test_matching_returns_nothing_for_an_unknown_symptom():
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    assert match_playbooks_by_symptoms(["no_such_symptom"], chunks) == []


def test_matching_is_deterministic_and_ordered_by_source_then_chunk():
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    first = match_playbooks_by_symptoms(["lcp_fail", "page_weight"], chunks)
    second = match_playbooks_by_symptoms(["page_weight", "lcp_fail"], chunks)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


# -- rule_based_analysis ---------------------------------------------------- #
def test_rule_based_analysis_produces_findings_from_symptoms():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    summary, findings, impacts, candidates = rule_based_analysis(run, symptoms, chunks)

    assert summary
    assert findings and all(isinstance(f, Finding) for f in findings)
    # every finding traces to a detected symptom
    detected = {s.code for s in symptoms}
    for finding in findings:
        assert set(finding.symptom_codes) <= detected


def test_rule_based_analysis_produces_impacts_for_each_audience():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    _, _, impacts, _ = rule_based_analysis(run, symptoms, chunks)
    assert {i.audience for i in impacts} == {"ux", "seo", "business"}
    assert all(isinstance(i, Impact) for i in impacts)


def test_rule_based_recommendations_cite_a_real_playbook_section():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    _, _, _, recommendations = rule_based_analysis(run, symptoms, chunks)

    assert recommendations
    assert all(isinstance(r, Recommendation) for r in recommendations)
    sources = {c.source for c in chunks}
    for rec in recommendations:
        assert rec.playbook_source in sources
        assert rec.playbook_section
        assert rec.effort in {"low", "medium", "high", "unknown"}


def test_rule_based_recommendations_are_capped_and_deterministic():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    first = rule_based_analysis(run, symptoms, chunks)[3]
    second = rule_based_analysis(run, symptoms, chunks)[3]
    assert len(first) <= 6
    assert [r.title for r in first] == [r.title for r in second]


def test_rule_based_analysis_on_a_healthy_run_says_so():
    run = make_run(lcp=1500, cls=0.01, inp=80)
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    summary, findings, impacts, recommendations = rule_based_analysis(
        run, symptoms, chunks
    )
    assert "no threshold" in summary.lower() or "within" in summary.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/findings_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analysis.findings'`

- [ ] **Step 3: Implement selection and the rule-based path**

Create `analysis/findings.py`:

```python
"""Per-page analysis: the LLM path and the path that works without one.

Two things live here that are easy to conflate and must not be:

* **Selection and validation** — which run represents a page, and whether a
  model's citation refers to a playbook that was actually retrieved. Rules,
  not judgement.
* **The rule-based path** — a complete analysis with no model at all, built
  from detected symptoms and front-matter symptom matching. It exists because
  a missing key or an exhausted free-tier quota should degrade the report, not
  destroy the campaign (§8 of the design spec).

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

_SEVERITY_ORDER = {"fail": 0, "warn": 1}

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


@dataclass(frozen=True)
class Finding:
    """One localized problem statement."""

    title: str
    detail: str = ""
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

    from rag.retrieve import detect_symptoms  # local: avoids an import cycle

    def key(run: Run):
        fails = sum(
            1 for s in detect_symptoms(run) if s.severity == "fail"
        )
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
            evidence=(
                (f"{symptom.metric}={symptom.value}",) if symptom.metric else ()
            ),
            symptom_codes=(symptom.code,),
        )
        for symptom in symptoms
    ]

    impacts = _impacts_for(symptoms)
    recommendations = _rule_based_recommendations(run, symptoms, chunks)
    return summary, findings, impacts, recommendations


def _finding_title(symptom: Symptom) -> str:
    """A short title for a symptom-derived finding."""
    metric_names = {
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
    if symptom.metric in metric_names:
        verb = "exceeds its target" if symptom.severity == "fail" else "is above target"
        return f"{metric_names[symptom.metric]} {verb}"
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
    return [Impact(audience=a, text=_DEFAULT_IMPACTS[a])
            for a in ("ux", "seo", "business")]


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
             chunk.source, chunk.heading_path[-1], chunk.metadata)
            for chunk in selected
        ],
        projections,
    )


def build_recommendations(
    rows: Sequence[Tuple[str, str, str, str, Mapping[str, Any]]],
    projections: Mapping[str, Sequence[Projection]],
) -> List[Recommendation]:
    """Assemble ordered recommendations from ``(title, rationale, source,
    section, metadata)`` rows and the projections grouped by source.

    Shared by both paths so LLM-authored and rule-based recommendations are
    ordered by exactly the same rule (§7.1).
    """
    from analysis.estimator import rank_key

    built = [
        Recommendation(
            title=title,
            rationale=rationale,
            playbook_source=source,
            playbook_section=section,
            effort=effort_of(metadata),
            projections=tuple(projections.get(source, ())),
        )
        for title, rationale, source, section, metadata in rows
    ]
    return sorted(
        built,
        key=lambda r: rank_key(r.playbook_source, r.title, r.projections),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/findings_test.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add analysis/findings.py tests/unit/findings_test.py
git commit -m "Add primary-run selection and the rule-based analysis path"
```

---

### Task 5: Findings — the LLM path with citation validation

**Files:**
- Modify: `analysis/findings.py`
- Test: `tests/unit/findings_test.py`

**Interfaces:**
- Consumes: everything from Task 4, plus `analysis.llm.LlmPageAnalysis`, `rag.prompt.build_analysis_prompt`, `store.vectordb.SearchHit`
- Produces:
  - `PageAnalysis` dataclass: `page_name, page_url, primary_run, runs, symptoms, summary, findings, impacts, recommendations, projections, mode, degradation_reason, dropped_recommendations, playbooks_cited`
  - `analyze_page(runs, *, hits, symptoms, client=None, prior_findings=(), thresholds=None, chunks=None) -> PageAnalysis`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/findings_test.py`:

```python
from analysis.findings import PageAnalysis, analyze_page
from analysis.llm import InvalidModelOutputError, LlmPageAnalysis
from rag.embeddings import QuotaExceededError
from store.vectordb import SearchHit


def a_hit(source="images.md", **meta):
    metadata = {"category": "images", "effort": "low",
                "expected_lcp_reduction_pct": [15, 40], "heading_path": ["Images"]}
    metadata.update(meta)
    return SearchHit(doc_id=f"{source}#x", text="Serve modern formats. Use AVIF.",
                     kind="knowledge", source=source, metadata=metadata, score=0.9)


class FakeClient:
    """Stands in for GoogleAnalysisClient."""

    model = "fake-llm"

    def __init__(self, page_result=None, error=None):
        self._page_result = page_result
        self._error = error
        self.calls = 0

    def analyze_page(self, prompt):
        self.calls += 1
        if self._error:
            raise self._error
        return self._page_result


def an_llm_result(source="images.md"):
    return LlmPageAnalysis.model_validate({
        "summary": "The hero video dominates the LCP path.",
        "findings": [{"title": "Hero video is the LCP element",
                      "detail": "2140KB before first paint.",
                      "evidence": ["lcp_ms=6200"],
                      "symptom_codes": ["lcp_fail", "invented_code"]}],
        "impacts": [{"audience": "ux", "text": "Empty hero for six seconds."}],
        "recommendations": [{"title": "Replace the video with a poster",
                             "rationale": "Removes 2MB from the critical path.",
                             "playbook_source": source,
                             "playbook_section": "Serve modern formats"}],
    })


def _setup():
    run = make_run()
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    return run, symptoms, chunks


def test_llm_path_produces_an_llm_mode_analysis():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[a_hit()], symptoms=symptoms,
                          client=FakeClient(an_llm_result()), chunks=chunks)
    assert isinstance(result, PageAnalysis)
    assert result.mode == "llm"
    assert result.degradation_reason is None
    assert result.summary.startswith("The hero video")
    assert result.playbooks_cited == ["images.md"]


def test_numbers_come_from_the_estimator_not_the_model():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[a_hit()], symptoms=symptoms,
                          client=FakeClient(an_llm_result()), chunks=chunks)
    rec = result.recommendations[0]
    assert rec.projections
    projection = rec.projections[0]
    assert projection.metric == "lcp_ms"
    assert projection.before == 6200.0
    assert projection.after_low == pytest.approx(6200 * 0.85)


def test_unknown_symptom_codes_are_pruned_but_the_finding_survives():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[a_hit()], symptoms=symptoms,
                          client=FakeClient(an_llm_result()), chunks=chunks)
    codes = result.findings[0].symptom_codes
    assert "lcp_fail" in codes
    assert "invented_code" not in codes


def test_a_recommendation_citing_an_unretrieved_playbook_is_dropped():
    run, symptoms, chunks = _setup()
    result = analyze_page(
        [run], hits=[a_hit("images.md")], symptoms=symptoms,
        client=FakeClient(an_llm_result(source="fabricated.md")), chunks=chunks
    )
    # every recommendation was dropped -> fell back for this page
    assert result.dropped_recommendations == 1
    assert result.mode == "rule_based"
    assert result.degradation_reason == "no_grounded_recommendations"


def test_partial_drop_keeps_the_grounded_recommendations():
    run, symptoms, chunks = _setup()
    mixed = LlmPageAnalysis.model_validate({
        "summary": "s",
        "findings": [],
        "impacts": [],
        "recommendations": [
            {"title": "Real", "rationale": "r", "playbook_source": "images.md",
             "playbook_section": "Serve modern formats"},
            {"title": "Fake", "rationale": "r", "playbook_source": "invented.md",
             "playbook_section": "Nope"},
        ],
    })
    result = analyze_page([run], hits=[a_hit("images.md")], symptoms=symptoms,
                          client=FakeClient(mixed), chunks=chunks)
    assert result.mode == "llm"
    assert result.dropped_recommendations == 1
    assert [r.title for r in result.recommendations] == ["Real"]


def test_no_client_falls_back_with_a_reason():
    run, symptoms, chunks = _setup()
    result = analyze_page([run], hits=[], symptoms=symptoms, client=None,
                          chunks=chunks)
    assert result.mode == "rule_based"
    assert result.degradation_reason == "no_api_key"
    assert result.recommendations


def test_invalid_model_output_falls_back_with_a_reason():
    run, symptoms, chunks = _setup()
    result = analyze_page(
        [run], hits=[a_hit()], symptoms=symptoms,
        client=FakeClient(error=InvalidModelOutputError("bad json")), chunks=chunks
    )
    assert result.mode == "rule_based"
    assert result.degradation_reason == "invalid_model_output"


def test_quota_exhaustion_falls_back_with_a_reason():
    run, symptoms, chunks = _setup()
    result = analyze_page(
        [run], hits=[a_hit()], symptoms=symptoms,
        client=FakeClient(error=QuotaExceededError("out")), chunks=chunks
    )
    assert result.mode == "rule_based"
    assert result.degradation_reason == "quota_exhausted"


def test_all_runs_for_the_page_are_retained_for_comparison():
    mobile = make_run("run_m", device="mid-mobile", network="slow-4g")
    desktop = make_run("run_d", lcp=2100, cls=0.02, inp=90,
                       device="desktop", network="fast-3g")
    _, symptoms, chunks = _setup()
    result = analyze_page([desktop, mobile], hits=[a_hit()], symptoms=symptoms,
                          client=FakeClient(an_llm_result()), chunks=chunks)
    assert result.primary_run.run_id == "run_m"
    assert [r.run_id for r in result.runs] == ["run_d", "run_m"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/findings_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'PageAnalysis' from 'analysis.findings'`

- [ ] **Step 3: Implement the LLM path**

Append to `analysis/findings.py`:

```python
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
) -> PageAnalysis:
    """Analyse one page, with a model when there is one and rules when not.

    ``hits`` are the retrieved playbook chunks; their ``source`` values are the
    *only* citations a recommendation may claim. ``chunks`` is the on-disk
    playbook corpus used by the fallback — passed in so tests and repeated
    pages do not re-read the directory.
    """
    from analysis.estimator import aggregate
    from analysis.llm import AnalysisError, InvalidModelOutputError
    from rag.embeddings import EmbeddingError, MissingApiKeyError, QuotaExceededError
    from rag.knowledge import load_knowledge_dir
    from rag.prompt import build_analysis_prompt

    ordered_runs = sorted(
        runs, key=lambda r: (r.condition.device, r.condition.network, r.run_id)
    )
    primary = select_primary(ordered_runs)
    corpus = list(chunks) if chunks is not None else load_knowledge_dir(knowledge_dir)

    if client is None:
        return _rule_based_page(ordered_runs, primary, list(symptoms), corpus,
                                "no_api_key")

    prompt = build_analysis_prompt(
        primary, hits, symptoms=symptoms, prior_findings=prior_findings
    )
    try:
        result = client.analyze_page(prompt)
    except QuotaExceededError:
        return _rule_based_page(ordered_runs, primary, list(symptoms), corpus,
                                "quota_exhausted")
    except MissingApiKeyError:
        return _rule_based_page(ordered_runs, primary, list(symptoms), corpus,
                                "no_api_key")
    except (InvalidModelOutputError, AnalysisError, EmbeddingError):
        return _rule_based_page(ordered_runs, primary, list(symptoms), corpus,
                                "invalid_model_output")

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
             allowed[rec.playbook_source])
            for rec in kept
        ],
        projections,
    )

    detected = {s.code for s in symptoms}
    findings = [
        Finding(
            title=f.title,
            detail=f.detail,
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/findings_test.py -v`
Expected: PASS, 21 tests

- [ ] **Step 5: Commit**

```bash
git add analysis/findings.py tests/unit/findings_test.py
git commit -m "Add LLM analysis path with citation validation and degradation"
```

---

### Task 6: Report model

**Files:**
- Create: `analysis/reportmodel.py`
- Test: `tests/unit/reportmodel_test.py`

**Interfaces:**
- Consumes: `PageAnalysis` from Task 5, `config.load.Settings`/`Thresholds`, `analysis.llm.LlmSummary`
- Produces:
  - Pydantic models `Cover`, `Summary`, `ConditionRow`, `PageBlock`, `ComparisonRow`, `Methodology`, `ReportMeta`, `Report`
  - `campaign_id(project: str, run_ids: Sequence[str]) -> str`
  - `verdict_for(symptoms) -> str`
  - `build_report(pages, *, project, settings, summary, generated_at, model, knowledge_digest) -> Report`
  - `to_json(report: Report) -> str`
  - `stable_payload(report: Report) -> dict` — volatile fields removed, for the determinism check

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/reportmodel_test.py`:

```python
"""Unit tests for analysis/reportmodel.py — assembly and determinism."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from analysis.findings import Finding, Impact, PageAnalysis, Recommendation
from analysis.estimator import Projection
from analysis.llm import LlmSummary
from analysis.reportmodel import (
    Report,
    build_report,
    campaign_id,
    stable_payload,
    to_json,
    verdict_for,
)
from config.load import Settings
from normalize.schema import Run
from rag import retrieve
from config.load import Thresholds


def make_run(run_id="run_a", page="homepage", lcp=6200, device="mid-mobile",
             network="slow-4g"):
    return Run.model_validate({
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com"},
        "page": {"name": page, "url": f"https://example.com/{page}"},
        "condition": {"device": device, "network": network,
                      "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": "2026-01-08T14:30:00Z", "source": "automated"},
        "metrics": {
            "cwp": {"lcp_ms": lcp, "cls": 0.42, "inp_ms": 480, "fcp_ms": 3100,
                    "ttfb_ms": 1800, "tbt_ms": 620},
            "network": {"total_transfer_kb": 4820, "request_count": 118,
                        "render_blocking_css": 6},
            "main_thread": {"script_ms": 1820, "task_ms": 3100, "dom_nodes": 3200},
        },
        "resource_timings": [
            {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140,
             "duration_ms": 390},
            {"name": "/app.js", "type": "script", "transfer_kb": 480,
             "duration_ms": 120},
        ],
        "captures": {"screenshot": "shot.png", "har": "capture.har"},
    })


def a_page(name="homepage", recommendations=None, mode="llm"):
    run = make_run(page=name)
    symptoms = retrieve.detect_symptoms(run, Thresholds())
    recs = recommendations if recommendations is not None else [
        Recommendation(
            title="Serve modern formats", rationale="AVIF is smaller.",
            playbook_source="images.md", playbook_section="Serve modern formats",
            effort="low",
            projections=(Projection("lcp_ms", 6200, 5270, 3720, 0.15, "images.md"),),
        )
    ]
    return PageAnalysis(
        page_name=name, page_url=run.page.url, primary_run=run, runs=[run],
        symptoms=symptoms, summary=f"{name} is slow.",
        findings=[Finding(title="LCP too high", detail="6200ms",
                          evidence=("lcp_ms=6200",), symptom_codes=("lcp_fail",))],
        impacts=[Impact(audience="ux", text="Empty hero.")],
        recommendations=recs,
        projections={"lcp_ms": Projection("lcp_ms", 6200, 5270, 3720, 0.15,
                                          "aggregate")},
        mode=mode, degradation_reason=None if mode == "llm" else "no_api_key",
        dropped_recommendations=0, playbooks_cited=["images.md"],
    )


def a_summary():
    return LlmSummary(problem="Slow storefront.", key_finding="Media weight.",
                      top_actions=["Compress the hero", "Preload fonts"])


def build(pages=None, **kwargs):
    kwargs.setdefault("project", "storefront")
    kwargs.setdefault("settings", Settings())
    kwargs.setdefault("summary", a_summary())
    kwargs.setdefault("generated_at", datetime(2026, 8, 2, 14, 30,
                                               tzinfo=timezone.utc))
    kwargs.setdefault("model", "gemini-2.0-flash")
    kwargs.setdefault("knowledge_digest", "abc123")
    return build_report(pages or [a_page()], **kwargs)


# -- campaign id ------------------------------------------------------------ #
def test_campaign_id_is_content_addressed_and_order_independent():
    assert campaign_id("storefront", ["b", "a"]) == campaign_id("storefront", ["a", "b"])


def test_campaign_id_changes_with_the_runs():
    assert campaign_id("storefront", ["a"]) != campaign_id("storefront", ["a", "b"])


def test_campaign_id_slugifies_the_project_name():
    assert campaign_id("My Store!", ["a"]).startswith("my-store-")


# -- verdict ---------------------------------------------------------------- #
def test_verdict_is_the_worst_severity_present():
    run = make_run()
    fails = retrieve.detect_symptoms(run, Thresholds())
    assert verdict_for(fails) == "fail"
    assert verdict_for([]) == "pass"


# -- assembly --------------------------------------------------------------- #
def test_report_has_every_section_of_the_fixed_skeleton():
    report = build()
    assert isinstance(report, Report)
    assert report.schema_version == 1
    for section in ("cover", "summary", "pages", "comparison", "methodology", "meta"):
        assert getattr(report, section) is not None


def test_cover_lists_pages_and_the_worst_verdict():
    report = build([a_page("plp"), a_page("homepage")])
    assert report.cover.pages == ["homepage", "plp"]
    assert report.cover.verdict == "fail"
    assert report.cover.project == "storefront"


def test_pages_are_ordered_by_name():
    report = build([a_page("plp"), a_page("homepage"), a_page("pdp")])
    assert [p.name for p in report.pages] == ["homepage", "pdp", "plp"]


def test_recommendations_keep_the_order_findings_gave_them():
    high = Recommendation(
        title="Big", rationale="", playbook_source="a.md", playbook_section="s",
        effort="low",
        projections=(Projection("lcp_ms", 6200, 4000, 3000, 0.35, "a.md"),))
    low = Recommendation(
        title="Small", rationale="", playbook_source="b.md", playbook_section="s",
        effort="high",
        projections=(Projection("cls", 0.42, 0.40, 0.35, 0.05, "b.md"),))
    report = build([a_page(recommendations=[high, low])])
    assert [r.title for r in report.pages[0].recommendations] == ["Big", "Small"]


def test_resources_are_ordered_heaviest_first():
    report = build()
    kb = [r.transfer_kb for r in report.pages[0].resources]
    assert kb == sorted(kb, reverse=True)


def test_resource_type_totals_are_summed():
    report = build()
    assert report.pages[0].resource_type_totals == {"media": 2140.0, "script": 480.0}


def test_comparison_row_per_condition_ordered():
    page = a_page()
    page.runs = [make_run("run_d", device="desktop", network="fast-3g"),
                 make_run("run_m", device="mid-mobile", network="slow-4g")]
    report = build([page])
    assert [(r.device, r.network) for r in report.comparison] == [
        ("desktop", "fast-3g"), ("mid-mobile", "slow-4g")
    ]


def test_meta_records_the_mode_and_cited_playbooks():
    report = build()
    assert report.meta.analysis_mode == "llm"
    assert report.meta.degradation_reason is None
    assert report.meta.playbooks_cited == ["images.md"]
    assert report.meta.knowledge_digest == "abc123"


def test_any_degraded_page_degrades_the_report_mode():
    report = build([a_page("homepage", mode="llm"), a_page("plp", mode="rule_based")])
    assert report.meta.analysis_mode == "rule_based"
    assert report.meta.degradation_reason == "no_api_key"


def test_methodology_lists_devices_networks_and_captures():
    report = build()
    assert report.methodology.devices == ["mid-mobile"]
    assert report.methodology.networks == ["slow-4g"]
    assert report.methodology.captures[0].screenshot == "shot.png"


def test_thresholds_come_from_settings_not_hard_coded():
    settings = Settings()
    settings.thresholds.lcp_good_ms = 1234
    report = build(settings=settings)
    assert report.methodology.thresholds["lcp_good_ms"] == 1234
    assert report.pages[0].targets["lcp_ms"] == 1234


# -- serialisation ---------------------------------------------------------- #
def test_to_json_round_trips():
    payload = json.loads(to_json(build()))
    assert payload["schema_version"] == 1
    assert payload["cover"]["campaign_id"]


def test_stable_payload_drops_generated_at():
    early = build(generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    late = build(generated_at=datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert stable_payload(early) == stable_payload(late)
    assert to_json(early) != to_json(late)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/reportmodel_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analysis.reportmodel'`

- [ ] **Step 3: Implement the report model**

Create `analysis/reportmodel.py`:

```python
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
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from analysis.estimator import Projection
from analysis.findings import PageAnalysis
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


def _page_block(page: PageAnalysis, settings: Settings) -> PageBlock:
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


def _comparison(pages: Sequence[PageAnalysis]) -> List[ComparisonRow]:
    rows: List[ComparisonRow] = []
    for page in pages:
        for run in page.runs:
            cwp = run.metrics.cwp
            rows.append(ComparisonRow(
                page=page.page_name, device=run.condition.device,
                network=run.condition.network, lcp_ms=cwp.lcp_ms, cls=cwp.cls,
                inp_ms=cwp.inp_ms, tbt_ms=cwp.tbt_ms,
                verdict=verdict_for(page.symptoms),
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
) -> Report:
    """Assemble the Report JSON from per-page analyses.

    ``summary`` is anything with ``problem``, ``key_finding`` and
    ``top_actions`` — an ``LlmSummary`` or the rule-based stand-in.
    """
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
        pages=[_page_block(p, settings) for p in ordered],
        comparison=_comparison(ordered),
        methodology=_methodology(ordered, settings),
        meta=ReportMeta(
            analysis_mode="rule_based" if degraded else "llm",
            degradation_reason=degraded[0].degradation_reason if degraded else None,
            model=model,
            playbooks_cited=sorted(
                {s for p in ordered for s in p.playbooks_cited}
            ),
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/reportmodel_test.py -v`
Expected: PASS, 18 tests

- [ ] **Step 5: Commit**

```bash
git add analysis/reportmodel.py tests/unit/reportmodel_test.py
git commit -m "Add Report JSON model with deterministic ordering"
```

---

### Task 7: CLI, findings persistence, and the determinism test

**Files:**
- Create: `analysis/__main__.py`
- Test: `tests/integration/analysis_pipeline_test.py`
- Modify: `docs/PROJECT_SPEC.md:476-479` (tick Phase 4 boxes)
- Modify: `README.md` (status, roadmap, an Analysis section)

**Interfaces:**
- Consumes: everything above, plus `store.sql`, `store.vectordb.SqliteVectorStore`/`Document`, `rag.embeddings.GoogleEmbeddingClient`, `rag.retrieve`, `rag.knowledge`, `config.load.load_settings`
- Produces:
  - `load_runs(input_dir=None, from_store=None, pages=None) -> List[Run]`
  - `group_by_page(runs) -> Dict[str, List[Run]]`
  - `rule_based_summary(pages) -> SimpleSummary`
  - `persist_findings(store, client, report, pages) -> int`
  - `run_analysis(...) -> Report`
  - `_build_parser()`, `main(argv=None) -> int`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/analysis_pipeline_test.py`:

```python
"""Integration tests for the analysis pipeline and its CLI.

Everything is offline: runs come from temp JSON files, the LLM and embedding
clients are fakes, and the vector store is in-memory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis.__main__ import (
    group_by_page,
    load_runs,
    main,
    persist_findings,
    rule_based_summary,
    run_analysis,
)
from analysis.llm import LlmPageAnalysis, LlmSummary
from analysis.reportmodel import Report, stable_payload
from normalize.schema import Run
from store import sql
from store.vectordb import SqliteVectorStore


def run_payload(run_id, page, device="mid-mobile", network="slow-4g", lcp=6200):
    return {
        "run_id": run_id,
        "project": {"name": "storefront", "url": "https://example.com"},
        "page": {"name": page, "url": f"https://example.com/{page}"},
        "condition": {"device": device, "network": network,
                      "cpu_throttle": 4, "runs": 3},
        "meta": {"created_at": "2026-01-08T14:30:00Z", "source": "automated"},
        "metrics": {
            "cwp": {"lcp_ms": lcp, "cls": 0.42, "inp_ms": 480, "fcp_ms": 3100,
                    "ttfb_ms": 1800, "tbt_ms": 620},
            "network": {"total_transfer_kb": 4820, "request_count": 118,
                        "render_blocking_css": 6},
            "main_thread": {"script_ms": 1820, "task_ms": 3100, "dom_nodes": 3200},
        },
        "resource_timings": [
            {"name": "/hero.mp4", "type": "media", "transfer_kb": 2140,
             "duration_ms": 390},
        ],
    }


@pytest.fixture
def input_dir(tmp_path):
    directory = tmp_path / "processed"
    directory.mkdir()
    for run_id, page in (("run_h1", "homepage"), ("run_h2", "homepage"),
                         ("run_p1", "plp")):
        device = "desktop" if run_id == "run_h2" else "mid-mobile"
        payload = run_payload(run_id, page, device=device)
        (directory / f"{run_id}.json").write_text(json.dumps(payload),
                                                  encoding="utf-8")
    return directory


class FakeEmbeddings:
    model = "fake-embed"

    def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeLlm:
    model = "fake-llm"

    def __init__(self):
        self.page_calls = 0
        self.summary_calls = 0

    def analyze_page(self, prompt):
        self.page_calls += 1
        source = next(
            (s for s in prompt.sources if s.endswith(".md")), "images.md"
        )
        return LlmPageAnalysis.model_validate({
            "summary": "Media weight dominates.",
            "findings": [{"title": "Hero media is heavy", "detail": "2140KB",
                          "evidence": ["lcp_ms=6200"],
                          "symptom_codes": ["lcp_fail"]}],
            "impacts": [{"audience": "ux", "text": "Empty hero."}],
            "recommendations": [{"title": "Compress the hero",
                                 "rationale": "Fewer bytes on the LCP path.",
                                 "playbook_source": source,
                                 "playbook_section": "Serve modern formats"}],
        })

    def summarize(self, payload):
        self.summary_calls += 1
        return LlmSummary(problem="Storefront is slow.",
                          key_finding="Media weight.",
                          top_actions=["Compress the hero"])


@pytest.fixture
def vector_store(tmp_path):
    from rag import knowledge
    conn = sql.connect(":memory:")
    store = SqliteVectorStore(conn, dim=4)
    knowledge.index_knowledge(store, FakeEmbeddings(), directory="data/knowledge")
    return store


# -- loading ---------------------------------------------------------------- #
def test_load_runs_reads_every_json_in_the_directory(input_dir):
    runs = load_runs(input_dir=input_dir)
    assert {r.run_id for r in runs} == {"run_h1", "run_h2", "run_p1"}


def test_load_runs_filters_by_page(input_dir):
    runs = load_runs(input_dir=input_dir, pages=["plp"])
    assert [r.run_id for r in runs] == ["run_p1"]


def test_load_runs_from_the_sqlite_store(tmp_path):
    db = tmp_path / "runs.sqlite"
    conn = sql.connect(db)
    sql.init_schema(conn)
    sql.insert_run(conn, Run.model_validate(run_payload("run_s1", "homepage")))
    conn.close()
    runs = load_runs(from_store=db)
    assert [r.run_id for r in runs] == ["run_s1"]


def test_load_runs_errors_when_nothing_is_found(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        load_runs(input_dir=empty)


def test_group_by_page_is_sorted(input_dir):
    grouped = group_by_page(load_runs(input_dir=input_dir))
    assert list(grouped) == ["homepage", "plp"]
    assert len(grouped["homepage"]) == 2


# -- pipeline --------------------------------------------------------------- #
def test_run_analysis_produces_a_report_for_every_page(input_dir, vector_store):
    llm = FakeLlm()
    report = run_analysis(load_runs(input_dir=input_dir), store=vector_store,
                          embed_client=FakeEmbeddings(), llm_client=llm)
    assert isinstance(report, Report)
    assert [p.name for p in report.pages] == ["homepage", "plp"]
    assert report.meta.analysis_mode == "llm"


def test_one_call_per_page_plus_one_summary(input_dir, vector_store):
    llm = FakeLlm()
    run_analysis(load_runs(input_dir=input_dir), store=vector_store,
                 embed_client=FakeEmbeddings(), llm_client=llm)
    assert llm.page_calls == 2      # homepage, plp
    assert llm.summary_calls == 1


def test_pipeline_is_deterministic(input_dir, vector_store):
    runs = load_runs(input_dir=input_dir)
    first = run_analysis(runs, store=vector_store, embed_client=FakeEmbeddings(),
                         llm_client=FakeLlm())
    second = run_analysis(runs, store=vector_store, embed_client=FakeEmbeddings(),
                          llm_client=FakeLlm())
    assert stable_payload(first) == stable_payload(second)


def test_no_clients_degrades_to_rule_based(input_dir):
    report = run_analysis(load_runs(input_dir=input_dir), store=None,
                          embed_client=None, llm_client=None)
    assert report.meta.analysis_mode == "rule_based"
    assert report.meta.degradation_reason == "no_api_key"
    assert report.pages[0].recommendations
    assert report.summary.problem


def test_rule_based_summary_names_the_worst_page(input_dir):
    from analysis.findings import analyze_page
    from rag import knowledge, retrieve
    runs = load_runs(input_dir=input_dir)
    chunks = knowledge.load_knowledge_dir("data/knowledge")
    pages = [
        analyze_page(group, hits=[], symptoms=retrieve.detect_symptoms(group[0]),
                     client=None, chunks=chunks)
        for group in group_by_page(runs).values()
    ]
    summary = rule_based_summary(pages)
    assert summary.problem
    assert 1 <= len(summary.top_actions) <= 3


# -- persistence ------------------------------------------------------------ #
def test_findings_are_persisted_as_finding_documents(input_dir, vector_store):
    runs = load_runs(input_dir=input_dir)
    llm = FakeLlm()
    report = run_analysis(runs, store=vector_store, embed_client=FakeEmbeddings(),
                          llm_client=llm, page_analyses_out=(collected := []))
    written = persist_findings(vector_store, FakeEmbeddings(), report, collected)
    assert written == 2
    hits = vector_store.query([1.0, 0.0, 0.0, 0.0], k=5, kind="finding")
    assert {h.doc_id for h in hits} == {
        f"finding:{report.cover.campaign_id}:homepage",
        f"finding:{report.cover.campaign_id}:plp",
    }


# -- CLI -------------------------------------------------------------------- #
def test_cli_writes_a_report_json(input_dir, tmp_path, capsys):
    out = tmp_path / "reports"
    code = main(["--input-dir", str(input_dir), "--output-dir", str(out),
                 "--no-llm"])
    assert code == 0
    written = list(out.glob("*/report.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["meta"]["analysis_mode"] == "rule_based"
    assert str(written[0]) in capsys.readouterr().out


def test_cli_rejects_conflicting_sources(input_dir, tmp_path):
    assert main(["--input-dir", str(input_dir), "--from-store",
                 str(tmp_path / "x.sqlite")]) != 0


def test_cli_reports_missing_runs_without_a_traceback(tmp_path, capsys):
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert main(["--input-dir", str(empty)]) != 0
    assert "No runs" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/analysis_pipeline_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analysis.__main__'`

- [ ] **Step 3: Implement the CLI and pipeline**

Create `analysis/__main__.py`:

```python
"""``python -m analysis`` — runs in, Report JSON out.

An interim entry point. Phase 6 folds it into a unified ``src/cli.py`` with
``ingest`` / ``analyze`` / ``report`` subcommands; until then this is how the
analysis layer is exercised by hand and by the integration tests.

It never fails because a model was unavailable. Missing key, exhausted quota
or unusable model output all degrade to the rule-based path and the report
says so in ``meta.analysis_mode``. A non-zero exit means something a user can
fix: no runs found, unreadable input, conflicting flags.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from analysis.findings import PageAnalysis, analyze_page
from analysis.reportmodel import Report, build_report, to_json
from config.load import load_settings
from normalize.schema import Run
from rag import knowledge, retrieve
from store.vectordb import Document

MAX_TOP_ACTIONS = 3


@dataclass
class SimpleSummary:
    """The rule-based stand-in for ``LlmSummary``."""

    problem: str
    key_finding: str
    top_actions: List[str]


def load_runs(
    *,
    input_dir: Optional[Any] = None,
    from_store: Optional[Any] = None,
    pages: Optional[Sequence[str]] = None,
) -> List[Run]:
    """Load runs from a directory of normalized JSON, or from SQLite."""
    runs: List[Run] = []
    if from_store is not None:
        from store import sql

        conn = sql.connect(from_store)
        sql.init_schema(conn)
        try:
            runs = sql.list_runs(conn)
        finally:
            conn.close()
    else:
        directory = Path(input_dir)
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Could not read run JSON {path}: {exc}") from exc
            runs.append(Run.model_validate(payload))

    if pages:
        wanted = {p.strip() for p in pages if p.strip()}
        runs = [r for r in runs if r.page.name in wanted]

    if not runs:
        where = from_store if from_store is not None else input_dir
        raise FileNotFoundError(f"No runs found in {where}")
    return sorted(runs, key=lambda r: r.run_id)


def group_by_page(runs: Sequence[Run]) -> Dict[str, List[Run]]:
    """Group runs by page name, page names sorted (§7.1)."""
    grouped: Dict[str, List[Run]] = {}
    for run in runs:
        grouped.setdefault(run.page.name, []).append(run)
    return {name: grouped[name] for name in sorted(grouped)}


def rule_based_summary(pages: Sequence[PageAnalysis]) -> SimpleSummary:
    """Executive summary without a model: state what the rules found."""
    failing = [p for p in pages if any(s.severity == "fail" for s in p.symptoms)]
    worst = failing[0] if failing else (pages[0] if pages else None)

    if worst is None:
        return SimpleSummary(
            problem="No runs were analysed.",
            key_finding="No measurements available.",
            top_actions=[],
        )

    page_word = "page" if len(pages) == 1 else "pages"
    problem = (
        f"{len(failing)} of {len(pages)} tested {page_word} exceed a Core Web "
        f"Vitals threshold." if failing else
        f"All {len(pages)} tested {page_word} are within their configured targets."
    )
    key_finding = worst.symptoms[0].text if worst.symptoms else worst.summary

    actions: List[str] = []
    for page in pages:
        for rec in page.recommendations:
            label = f"{rec.title} ({page.page_name})"
            if label not in actions:
                actions.append(label)
    return SimpleSummary(problem=problem, key_finding=key_finding,
                         top_actions=actions[:MAX_TOP_ACTIONS])


def _summary_payload(pages: Sequence[PageAnalysis]) -> str:
    """What the summary call sees: only text this system already produced.

    Neutralised anyway — it originated from a model that read untrusted
    context (design spec §12).
    """
    from rag.prompt import neutralize

    lines: List[str] = []
    for page in pages:
        lines.append(f"# {page.page_name}")
        lines.append(neutralize(page.summary))
        for finding in page.findings:
            lines.append(f"- {neutralize(finding.title)}")
        for rec in page.recommendations:
            lines.append(f"* action: {neutralize(rec.title)}")
    return "\n".join(lines)


def _top_up_actions(summary: Any, pages: Sequence[PageAnalysis]) -> Any:
    """Fill ``top_actions`` from the highest-projected recommendations.

    Never pads with invented actions: if the campaign has fewer than three
    recommendations, the list is simply shorter (design spec §5.4).
    """
    actions = list(summary.top_actions)
    for page in pages:
        if len(actions) >= MAX_TOP_ACTIONS:
            break
        for rec in page.recommendations:
            if len(actions) >= MAX_TOP_ACTIONS:
                break
            if rec.title not in actions:
                actions.append(rec.title)
    summary.top_actions = actions[:MAX_TOP_ACTIONS]
    return summary


def run_analysis(
    runs: Sequence[Run],
    *,
    store: Optional[Any] = None,
    embed_client: Optional[Any] = None,
    llm_client: Optional[Any] = None,
    settings: Optional[Any] = None,
    use_priors: bool = False,
    top_k: Optional[int] = None,
    knowledge_dir: str = "data/knowledge",
    generated_at: Optional[datetime] = None,
    page_analyses_out: Optional[List[PageAnalysis]] = None,
) -> Report:
    """Run the full analysis pipeline over a campaign's runs."""
    settings = settings or load_settings()
    k = top_k or settings.rag.top_k
    chunks = knowledge.load_knowledge_dir(knowledge_dir)
    digest = knowledge.content_digest(chunks)

    analyses: List[PageAnalysis] = []
    for _page_name, page_runs in group_by_page(runs).items():
        from analysis.findings import select_primary

        primary = select_primary(page_runs)
        symptoms = retrieve.detect_symptoms(primary, settings.thresholds)

        hits: List[Any] = []
        priors: List[Any] = []
        if store is not None and embed_client is not None:
            hits, _query = retrieve.retrieve_context(
                primary, store, embed_client,
                thresholds=settings.thresholds, top_k=k,
            )
            if use_priors:
                priors = retrieve.retrieve_prior_findings(
                    primary, store, embed_client, thresholds=settings.thresholds
                )

        analyses.append(analyze_page(
            page_runs, hits=hits, symptoms=symptoms, client=llm_client,
            prior_findings=priors, chunks=chunks,
        ))

    summary: Any = rule_based_summary(analyses)
    if llm_client is not None and all(p.mode == "llm" for p in analyses):
        from analysis.llm import AnalysisError
        from rag.embeddings import EmbeddingError

        try:
            summary = _top_up_actions(
                llm_client.summarize(_summary_payload(analyses)), analyses
            )
        except (AnalysisError, EmbeddingError):
            summary = rule_based_summary(analyses)

    if page_analyses_out is not None:
        page_analyses_out.extend(analyses)

    project = runs[0].project.name if runs else "report"
    model = getattr(llm_client, "model", "none") if llm_client else "none"
    return build_report(
        analyses, project=project, settings=settings, summary=summary,
        generated_at=generated_at or datetime.now(timezone.utc),
        model=model, knowledge_digest=digest,
    )


def persist_findings(
    store: Any, embed_client: Any, report: Report, pages: Sequence[PageAnalysis]
) -> int:
    """Embed each page's findings so future runs can retrieve them (§5.1.2)."""
    documents: List[Document] = []
    for page in pages:
        body = [page.summary]
        body += [f"{f.title}. {f.detail}" for f in page.findings]
        documents.append(Document(
            doc_id=f"finding:{report.cover.campaign_id}:{page.page_name}",
            text="\n".join(part for part in body if part),
            kind="finding",
            source=f"{report.cover.project}/{page.page_name}",
            metadata={
                "campaign_id": report.cover.campaign_id,
                "page": page.page_name,
                "run_id": page.primary_run.run_id,
                "created_at": report.cover.generated_at.isoformat(),
                "symptom_codes": [s.code for s in page.symptoms],
            },
        ))
    if not documents:
        return 0
    vectors = embed_client.embed_documents([d.text for d in documents])
    store.add(documents, vectors, model=embed_client.model)
    return len(documents)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m analysis",
        description="Analyse stored runs and emit the Report JSON.",
    )
    p.add_argument("--input-dir", default=None,
                   help="Directory of normalized run JSON (default data/processed).")
    p.add_argument("--from-store", default=None,
                   help="Read runs from this SQLite database instead of a directory.")
    p.add_argument("--pages", default=None,
                   help="Comma-separated page names to analyse.")
    p.add_argument("--output-dir", default=None,
                   help="Where to write <campaign-id>/report.json.")
    p.add_argument("--no-llm", action="store_true",
                   help="Force the rule-based path; make no model calls.")
    p.add_argument("--use-priors", action="store_true",
                   help="Ground analysis in findings from previous campaigns.")
    p.add_argument("--top-k", type=int, default=None,
                   help="Playbook chunks to retrieve per page.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.input_dir and args.from_store:
        print("--input-dir and --from-store are mutually exclusive.",
              file=sys.stderr)
        return 2

    settings = load_settings()
    output_dir = Path(args.output_dir or settings.report.output_dir)
    pages = args.pages.split(",") if args.pages else None

    try:
        runs = load_runs(
            input_dir=args.input_dir or ("data/processed" if not args.from_store
                                         else None),
            from_store=args.from_store,
            pages=pages,
        )
    except FileNotFoundError as exc:
        print(f"No runs to analyse: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    store = embed_client = llm_client = None
    if not args.no_llm:
        store, embed_client, llm_client = _build_live_clients(settings)

    collected: List[PageAnalysis] = []
    report = run_analysis(
        runs, store=store, embed_client=embed_client, llm_client=llm_client,
        settings=settings, use_priors=args.use_priors, top_k=args.top_k,
        page_analyses_out=collected,
    )

    target = output_dir / report.cover.campaign_id
    try:
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "report.json"
        destination.write_text(to_json(report), encoding="utf-8")
    except OSError as exc:
        print(f"Could not write the report: {exc}", file=sys.stderr)
        return 1

    if store is not None and embed_client is not None and report.meta.analysis_mode == "llm":
        try:
            persist_findings(store, embed_client, report, collected)
        except Exception as exc:  # persistence must never lose the report
            print(f"Findings were not persisted: {exc}", file=sys.stderr)

    print(destination)
    print(
        f"{len(report.pages)} page(s), verdict={report.cover.verdict}, "
        f"mode={report.meta.analysis_mode}"
    )
    return 0


def _build_live_clients(settings) -> tuple:
    """Build the real store and clients, or fall back to the rule-based path.

    A missing key is not an error here: it means this campaign is analysed by
    rules, which is a supported outcome.
    """
    from rag.embeddings import EmbeddingError, GoogleEmbeddingClient
    from store import sql
    from store.vectordb import SqliteVectorStore

    from analysis.llm import GoogleAnalysisClient

    try:
        from rag.embeddings import resolve_api_key

        resolve_api_key()
    except EmbeddingError as exc:
        print(f"Running rule-based: {exc}", file=sys.stderr)
        return None, None, None

    conn = sql.connect(settings.storage.sqlite_path)
    store = SqliteVectorStore(conn, dim=settings.models.embed_dimensions)
    embed_client = GoogleEmbeddingClient(model=settings.models.embeddings)
    llm_client = GoogleAnalysisClient(model=settings.models.llm)
    return store, embed_client, llm_client


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
```

- [ ] **Step 4: Run the integration tests**

Run: `pytest tests/integration/analysis_pipeline_test.py -v`
Expected: PASS, 14 tests

- [ ] **Step 5: Run the whole offline suite with coverage**

Run: `pytest -m "not e2e" --cov --cov-report=term-missing`
Expected: all green, coverage ≥80%. If `analysis/__main__.py` drags coverage down, add a test that calls `main()` with `--pages` and one that exercises `--use-priors` against the fake store rather than lowering the bar.

- [ ] **Step 6: Tick the spec's Phase 4 boxes**

In `docs/PROJECT_SPEC.md`, change the three Phase 4 lines (around line 477) from `- [ ]` to `- [x]`.

- [ ] **Step 7: Update the README**

Three edits, all required by the repo's rule that the README always states where the project is:

1. "Where the project is" — move analysis out of the missing table into the working list: `analysis/ — findings, impact statements, improvement estimator, Report JSON`. The remaining gaps are `report/` and `src/cli.py`.
2. Roadmap table — Phase 4 `Done`, Phase 5 `**Next**`.
3. Add a short "The analysis layer" section after "The RAG layer" covering: `python -m analysis`, the Report JSON as Phase 5's input, the estimator's grounding in playbook front matter, and the rule-based degradation with `meta.analysis_mode`.

- [ ] **Step 8: Verify nothing secret is staged**

Run: `git status --short && git diff --cached --name-only | grep -c "^\.env$" || true`
Expected: no `.env`, no `data/reports/*`, no `*.sqlite`.

- [ ] **Step 9: Commit**

```bash
git add analysis/__main__.py tests/integration/analysis_pipeline_test.py \
        docs/PROJECT_SPEC.md README.md
git commit -m "Add analysis CLI, findings persistence and determinism test"
```

---

## Self-Review

**Spec coverage** — every section of the design spec maps to a task:

| Spec section | Task |
|---|---|
| §3 module boundaries | 1–7 (one module per task group) |
| §4 data flow, §4.1 primary selection, §4.2 campaign id | 4 (selection), 6 (campaign id), 7 (flow) |
| §5.1 output shape, §5.2 validation, §5.3 client | 3 (shape/client), 5 (citation validation) |
| §5.4 summary call + top_actions rule | 7 (`_top_up_actions`, `rule_based_summary`) |
| §6.1 range parsing, §6.2 math, §6.3 output | 1, 2 |
| §7 Report JSON, §7.1 ordering, §7.2 verdict | 6 |
| §8 degradation table | 5 (all four triggers tested) |
| §9 findings persistence | 7 (`persist_findings`) |
| §10 CLI | 7 |
| §11 testing | every task's test step |
| §12 security | 2 step 5 (purity), 6 (`_slug`), 7 (`_summary_payload` neutralisation, secret check) |
| §13 definition of done | 7 steps 5–8 |

**Placeholder scan** — no TBD/TODO; every code step carries the real implementation; every test step carries real assertions.

**Type consistency** — checked across tasks: `Candidate`/`Projection` (Tasks 1–2) are consumed with the same field names in Tasks 4–6; `by_source`/`project`/`aggregate`/`rank_key` signatures match their call sites; `PageAnalysis` field names in Task 5 match the reads in Task 6's `_page_block`; `LlmSummary.top_actions` (Task 3) matches `_top_up_actions` and `Summary` (Tasks 6–7); `SimpleSummary` exposes the same three attributes `build_report` reads off `LlmSummary`.

Two things fixed during review: `build_recommendations` was originally private to the rule-based path but is needed by both, so it is defined in Task 4 and reused in Task 5; and `_metrics_map` likewise moved to module scope in Task 4 because Task 5 and the fallback both need it.
