# Readable Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the report into one document a project manager can read start to finish and act on, without taking evidence away from the developer doing the work.

**Architecture:** A reader layer over data the pipeline already collects. A committed glossary supplies each metric's plain-language gloss, rounding and target; a deterministic ranker produces one cross-page action plan from projections the estimator already computes; two plain-language fields join the model's output contract. The renderers are then restructured plain-language-first, evidence-after, and the skeleton baseline is regenerated as a reviewable diff.

**Tech Stack:** Python 3.11+, Pydantic v2, Jinja2 templates (`report/template/`), WeasyPrint (PDF), pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-readable-report-design.md`

## Global Constraints

- **Nothing about measurement or the estimator changes.** Every number in the report still comes from where it comes from today.
- **Determinism (PROJECT_SPEC §6.2):** two runs over the same campaign must render byte-identical documents. Every sort must have a total order — ties break on page name, then title.
- **No section is ever conditionally omitted.** An empty list renders its empty state; a vanishing section is the drift `report/skeleton.py` exists to catch.
- **The model still supplies no numbers.** Magnitudes come from playbook metadata via `analysis/estimator.py`; the citation guard in `analysis/findings.py` is unchanged.
- **Backwards compatible Report JSON:** every new field defaults (`action_plan=[]`, `consequence=""`, `why_it_matters=""`) so a `report.json` written before this change still validates and renders.
- Glossary lookups never raise: an unglossed metric renders exactly as it does today.

---

### Task 1: The glossary

**Files:**
- Create: `data/knowledge/glossary.yaml`
- Create: `report/glossary.py`
- Test: `tests/unit/glossary_test.py`

**Interfaces:**
- Consumes: `config.load.Thresholds` (for `target_key` resolution).
- Produces: `report.glossary.Glossary` with `load_glossary(path=None) -> Glossary`, and methods `label(metric) -> str`, `gloss(metric) -> str`, `format_value(metric, value) -> str`, `target_for(metric, thresholds) -> Optional[float]`, `context(metric, value, target) -> str`, `has(metric) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/glossary_test.py`:

```python
"""Unit tests for report/glossary.py — the committed plain-language layer."""
from __future__ import annotations

import pytest

from config.load import Thresholds
from report.glossary import load_glossary


@pytest.fixture(scope="module")
def gl():
    return load_glossary()


def test_every_metric_the_report_renders_has_an_entry(gl):
    """The at-a-glance table draws from these; a gap renders a bare number."""
    for metric in ("lcp_ms", "cls", "inp_ms", "fcp_ms", "ttfb_ms", "tbt_ms"):
        assert gl.has(metric), metric
        assert gl.gloss(metric)
        assert gl.label(metric)


def test_milliseconds_render_as_whole_numbers(gl):
    assert gl.format_value("lcp_ms", 2438.5999999940395) == "2439 ms"


def test_cls_renders_to_two_decimals(gl):
    assert gl.format_value("cls", 0.015911182251991944) == "0.02"


def test_an_unglossed_metric_falls_back_rather_than_raising(gl):
    assert gl.has("made_up_metric") is False
    assert gl.format_value("made_up_metric", 12.5) == "12.5"
    assert gl.label("made_up_metric") == "made_up_metric"
    assert gl.gloss("made_up_metric") == ""


def test_a_missing_value_renders_as_a_dash(gl):
    assert gl.format_value("lcp_ms", None) == "—"


def test_targets_come_from_the_configured_thresholds(gl):
    thresholds = Thresholds()
    assert gl.target_for("lcp_ms", thresholds) == float(thresholds.lcp_good_ms)
    assert gl.target_for("cls", thresholds) == float(thresholds.cls_good)


def test_a_metric_with_no_configured_target_has_none(gl):
    assert gl.target_for("tbt_ms", Thresholds()) is None


def test_context_states_how_far_over_target_a_value_is(gl):
    assert gl.context("lcp_ms", 5000.0, 2500.0) == "2.0× over"
    assert gl.context("lcp_ms", 2000.0, 2500.0) == "within target"
    assert gl.context("lcp_ms", 2600.0, 2500.0) == "1.0× over"


def test_context_is_empty_without_a_target(gl):
    assert gl.context("tbt_ms", 2041.0, None) == ""


def test_context_handles_a_zero_target_without_dividing_by_zero(gl):
    assert gl.context("cls", 0.4, 0.0) == "over target"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/glossary_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'report.glossary'`.

- [ ] **Step 3: Write the glossary data**

Create `data/knowledge/glossary.yaml`:

```yaml
# Plain-language layer for the report (design spec 2026-08-20).
#
# These sentences are committed, not model-written, so the same metric reads
# identically in every campaign — the comparability the fixed skeleton exists
# for. `target_key` names a field on config.load.Thresholds so targets stay
# configured in exactly one place; null means no threshold is configured and
# the context column stays empty rather than inventing one.
lcp_ms:
  label: Largest contentful paint
  unit: ms
  round: integer
  target_key: lcp_good_ms
  plain: >-
    How long until the main thing on the page — usually the big image or
    headline — has finished drawing.
cls:
  label: Layout shift
  unit: ""
  round: two_decimals
  target_key: cls_good
  plain: >-
    How much the page jumps around while it loads. Content moving under a
    finger is how people tap the wrong thing.
inp_ms:
  label: Interaction response
  unit: ms
  round: integer
  target_key: inp_good_ms
  plain: >-
    How long the page takes to visibly react after someone taps or clicks.
fcp_ms:
  label: First paint
  unit: ms
  round: integer
  target_key: fcp_good_ms
  plain: >-
    How long the visitor stares at a blank screen before anything at all
    appears.
ttfb_ms:
  label: Server response
  unit: ms
  round: integer
  target_key: ttfb_good_ms
  plain: >-
    How long the server takes to send the first byte of the page. Everything
    else waits on this.
tbt_ms:
  label: Total blocking time
  unit: ms
  round: integer
  target_key: null
  plain: >-
    How long the page ignores taps and clicks after it first appears. The page
    looks ready and does nothing.
total_transfer_kb:
  label: Page weight
  unit: kB
  round: integer
  target_key: null
  plain: >-
    How much has to be downloaded to show the page. On a phone connection this
    is time.
request_count:
  label: Requests
  unit: ""
  round: integer
  target_key: null
  plain: >-
    How many separate files the page asks for. Each one costs a round trip.
render_blocking_css:
  label: Render-blocking stylesheets
  unit: ""
  round: integer
  target_key: null
  plain: >-
    Stylesheets the browser must finish downloading before it will show
    anything at all.
script_ms:
  label: Script execution
  unit: ms
  round: integer
  target_key: null
  plain: >-
    How long the browser spends running JavaScript instead of drawing the page.
dom_nodes:
  label: Page elements
  unit: ""
  round: integer
  target_key: null
  plain: >-
    How many elements the page builds. A very large number makes every style
    change expensive.
```

- [ ] **Step 4: Write `report/glossary.py`**

```python
"""The committed plain-language layer over the report's metrics.

Every sentence here is data, not model output. That is the point: a model
asked to explain "total blocking time" writes it differently every run, and
the project's headline promise is that two campaigns produce comparable
documents. The model still writes the narrative that ties findings together;
it never writes what a metric *is*.

Rounding lives here too, because the raw values are floats measured to
microsecond precision and every renderer was printing them verbatim
(``2438.5999999940395``). One formatter, shared by all three renderers, fixes
that at the only point they have in common.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

DEFAULT_GLOSSARY_PATH = Path("data/knowledge/glossary.yaml")

#: What a value with no measurement renders as, everywhere.
MISSING = "—"


class GlossaryError(Exception):
    """The glossary file is missing or malformed."""


@dataclass(frozen=True)
class MetricGloss:
    label: str
    unit: str
    round: str
    target_key: Optional[str]
    plain: str


class Glossary:
    """Plain names, plain sentences, rounding and targets, by metric key."""

    def __init__(self, entries: Dict[str, MetricGloss]) -> None:
        self._entries = entries

    def has(self, metric: str) -> bool:
        return metric in self._entries

    def label(self, metric: str) -> str:
        """The display name, falling back to the raw key."""
        entry = self._entries.get(metric)
        return entry.label if entry else metric

    def gloss(self, metric: str) -> str:
        """One plain sentence, or empty for an unglossed metric."""
        entry = self._entries.get(metric)
        return entry.plain.strip() if entry else ""

    def format_value(self, metric: str, value: Optional[float]) -> str:
        """Round and unit-suffix a measurement for display."""
        if value is None:
            return MISSING
        entry = self._entries.get(metric)
        if entry is None:
            return f"{value}"
        if entry.round == "integer":
            shown = f"{round(float(value)):d}"
        elif entry.round == "two_decimals":
            shown = f"{float(value):.2f}"
        else:
            shown = f"{value}"
        return f"{shown} {entry.unit}".strip()

    def target_for(self, metric: str, thresholds: Any) -> Optional[float]:
        """The configured target for a metric, or None when none is set."""
        entry = self._entries.get(metric)
        if entry is None or not entry.target_key:
            return None
        value = getattr(thresholds, entry.target_key, None)
        return None if value is None else float(value)

    def context(self, metric: str, value: Optional[float],
                target: Optional[float]) -> str:
        """How the measurement stands against its target, in words.

        Empty when there is no target: an unconfigured threshold must not be
        reported as a pass, and inventing one would put a number in the report
        that nothing measured.
        """
        if value is None or target is None:
            return ""
        if float(value) <= float(target):
            return "within target"
        if float(target) == 0:
            return "over target"
        return f"{float(value) / float(target):.1f}× over"


def load_glossary(path: Optional[Path] = None) -> Glossary:
    """Load and validate the glossary file."""
    source = Path(path) if path else DEFAULT_GLOSSARY_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise GlossaryError(f"Could not read the glossary at {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise GlossaryError(f"The glossary at {source} is not valid YAML: {exc}") from exc

    entries: Dict[str, MetricGloss] = {}
    for metric, body in raw.items():
        if not isinstance(body, dict):
            raise GlossaryError(f"Glossary entry {metric!r} must be a mapping.")
        entries[str(metric)] = MetricGloss(
            label=str(body.get("label", metric)),
            unit=str(body.get("unit", "") or ""),
            round=str(body.get("round", "raw")),
            target_key=body.get("target_key") or None,
            plain=str(body.get("plain", "")),
        )
    return Glossary(entries)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/glossary_test.py -v`
Expected: PASS (10 tests).

- [ ] **Step 6: Commit**

```bash
git add data/knowledge/glossary.yaml report/glossary.py tests/unit/glossary_test.py
git commit -m "Add the committed plain-language glossary"
```

---

### Task 2: The cross-page action plan

**Files:**
- Create: `analysis/priority.py`
- Modify: `analysis/reportmodel.py` (add `PlannedAction`, `Report.action_plan`, wire into `build_report`)
- Test: `tests/unit/priority_test.py`

**Interfaces:**
- Consumes: `report.glossary.Glossary` (Task 1) for `format_value`; `analysis.reportmodel.PageBlock`, `RecommendationModel`, `ProjectionModel`, `SymptomModel`.
- Produces: `analysis.reportmodel.PlannedAction` (fields `rank: int`, `page: str`, `title: str`, `why_it_matters: str`, `effort: str`, `metric: Optional[str]`, `projected: str`, `playbook_source: str`), `Report.action_plan: List[PlannedAction]`, and `analysis.priority.rank_actions(pages, *, glossary, thresholds) -> List[PlannedAction]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/priority_test.py`:

```python
"""Unit tests for analysis/priority.py — one ordered plan across all pages."""
from __future__ import annotations

from config.load import Thresholds
from analysis.priority import rank_actions
from analysis.reportmodel import (
    PageBlock,
    ProjectionModel,
    RecommendationModel,
    SymptomModel,
)
from report.glossary import load_glossary


def _page(name, *, metric, value, severity, gain, title, effort="medium"):
    """A page carrying exactly one recommendation with one projection."""
    return PageBlock(
        name=name, url=f"https://example.com/{name}", primary_run_id=f"run_{name}",
        verdict="fail",
        conditions=[], metrics={metric: value}, targets={},
        symptoms=[SymptomModel(code=f"{metric}_x", text="t", severity=severity,
                               metric=metric, value=value, target=None)],
        resources=[], resource_type_totals={},
        summary="s", findings=[], impacts=[],
        recommendations=[RecommendationModel(
            title=title, rationale="r", playbook_source="javascript.md",
            playbook_section="s", effort=effort, magnitude="medium",
            projections=[ProjectionModel(metric=metric, before=value,
                                         after_low=value - gain,
                                         after_high=value - gain,
                                         reduction_pct=10.0,
                                         source="javascript.md")],
        )],
    )


def test_the_worse_page_outranks_the_better_one():
    """The live campaign ranked a 2041ms homepage action above an 8636ms PDP
    one, because pages were walked in alphabetical order."""
    pages = [
        _page("homepage", metric="tbt_ms", value=2041.0, severity="fail",
              gain=408.0, title="Break up long tasks"),
        _page("pdp", metric="tbt_ms", value=8636.0, severity="fail",
              gain=1727.0, title="Defer third-party scripts"),
    ]

    plan = rank_actions(pages, glossary=load_glossary(), thresholds=Thresholds())

    assert [a.page for a in plan] == ["pdp", "homepage"]
    assert [a.rank for a in plan] == [1, 2]


def test_severity_outweighs_a_marginally_larger_gain():
    pages = [
        _page("a", metric="tbt_ms", value=1000.0, severity="warn", gain=200.0,
              title="Warn action"),
        _page("b", metric="tbt_ms", value=1000.0, severity="fail", gain=150.0,
              title="Fail action"),
    ]

    plan = rank_actions(pages, glossary=load_glossary(), thresholds=Thresholds())

    assert plan[0].page == "b"


def test_a_gain_is_capped_at_the_gap_to_target():
    """Improving a metric past its target earns no extra credit."""
    pages = [
        _page("a", metric="lcp_ms", value=2600.0, severity="fail", gain=2000.0,
              title="Huge claimed gain, small real gap"),
        _page("b", metric="lcp_ms", value=6000.0, severity="fail", gain=1000.0,
              title="Real gap"),
    ]

    plan = rank_actions(pages, glossary=load_glossary(), thresholds=Thresholds())

    assert plan[0].page == "b"


def test_ties_break_on_page_then_title_so_runs_are_identical():
    pages = [
        _page("b", metric="tbt_ms", value=1000.0, severity="fail", gain=100.0,
              title="Zebra"),
        _page("a", metric="tbt_ms", value=1000.0, severity="fail", gain=100.0,
              title="Apple"),
    ]

    first = rank_actions(pages, glossary=load_glossary(), thresholds=Thresholds())
    second = rank_actions(list(reversed(pages)), glossary=load_glossary(),
                          thresholds=Thresholds())

    assert [(a.page, a.title) for a in first] == [("a", "Apple"), ("b", "Zebra")]
    assert [(a.page, a.title) for a in first] == [(a.page, a.title) for a in second]


def test_projected_change_is_rendered_ready_to_print():
    pages = [_page("a", metric="tbt_ms", value=2041.0, severity="fail",
                   gain=408.0, title="Break up long tasks")]

    plan = rank_actions(pages, glossary=load_glossary(), thresholds=Thresholds())

    assert plan[0].projected == "2041 ms → 1633 ms"
    assert plan[0].metric == "tbt_ms"


def test_a_recommendation_without_projections_still_makes_the_plan():
    """Rule-based campaigns have no magnitudes; they still have actions."""
    page = PageBlock(
        name="a", url="https://example.com/a", primary_run_id="run_a",
        verdict="fail", conditions=[], metrics={}, targets={},
        symptoms=[], resources=[], resource_type_totals={},
        summary="s", findings=[], impacts=[],
        recommendations=[RecommendationModel(
            title="Do the thing", rationale="r", playbook_source="caching.md",
            playbook_section="s", effort="low", magnitude="unknown",
            projections=[])],
    )

    plan = rank_actions([page], glossary=load_glossary(), thresholds=Thresholds())

    assert [a.title for a in plan] == ["Do the thing"]
    assert plan[0].projected == ""


def test_no_recommendations_is_an_empty_plan_not_a_crash():
    page = PageBlock(
        name="a", url="https://example.com/a", primary_run_id="run_a",
        verdict="pass", conditions=[], metrics={}, targets={}, symptoms=[],
        resources=[], resource_type_totals={}, summary="s", findings=[],
        impacts=[], recommendations=[],
    )

    assert rank_actions([page], glossary=load_glossary(),
                        thresholds=Thresholds()) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/priority_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analysis.priority'`.

- [ ] **Step 3: Add `PlannedAction` and `Report.action_plan`**

In `analysis/reportmodel.py`, beside `RecommendationModel`:

```python
class PlannedAction(BaseModel):
    """One entry in the campaign-wide plan, ordered by expected payoff.

    A flattened, display-ready view of a recommendation: the reader is asking
    "what do I do first", not "what did page three say".
    """

    rank: int
    page: str
    title: str
    why_it_matters: str = ""
    effort: str = ""
    metric: Optional[str] = None
    #: Pre-formatted ("2041 ms → 1633 ms"); empty when nothing was projected.
    projected: str = ""
    playbook_source: str = ""
```

On `Report`, defaulted so older files still validate:

```python
    action_plan: List[PlannedAction] = Field(default_factory=list)
```

- [ ] **Step 4: Write `analysis/priority.py`**

```python
"""One ordered plan across every page in the campaign.

The report used to present three per-page lists, and the executive summary took
its top three from whichever page sorted first alphabetically. On the live
Oakley campaign that put a 2041 ms homepage action above an 8636 ms blocking
time on the PDP — the reader's first question, "what do I fix first", answered
by an accident of sort order.

Scoring is entirely rule-based, over projections ``analysis/estimator.py``
already computed. The model does not order this list: ordering is a claim about
magnitude, and magnitudes in this project come from playbook metadata (§11).
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

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
    """How far over target the metric is, or None when no target is set."""
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

    # Descending by score, then by page and title: a total order, because two
    # runs of one campaign must render identical documents (§6.2).
    scored.sort(key=lambda row: (-row[0], row[1].name, row[2].title))

    plan: List[PlannedAction] = []
    for index, (_score, page, recommendation) in enumerate(scored, start=1):
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/priority_test.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add analysis/priority.py analysis/reportmodel.py tests/unit/priority_test.py
git commit -m "Rank every action across the campaign, not per page"
```

---

### Task 3: Wire the plan into the report and the summary

**Files:**
- Modify: `analysis/reportmodel.py` (`build_report`)
- Test: `tests/unit/reportmodel_test.py`

**Interfaces:**
- Consumes: `analysis.priority.rank_actions` (Task 2), `report.glossary.load_glossary` (Task 1).
- Produces: `Report.action_plan` populated; `Report.summary.top_actions` drawn from the plan's first three.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/reportmodel_test.py` (use the module's existing helpers for building `PageAnalysis` inputs — copy whichever factory the file already uses; do not invent a new one):

```python
def test_the_report_carries_a_ranked_plan():
    report = _report_with_two_pages()   # module's existing two-page helper

    assert [a.rank for a in report.action_plan] == list(
        range(1, len(report.action_plan) + 1))


def test_top_actions_come_from_the_plan_not_from_page_order():
    report = _report_with_two_pages()

    assert report.summary.top_actions[:3] == [
        f"{a.title} ({a.page})" for a in report.action_plan[:3]
    ]


def test_a_report_json_without_a_plan_still_validates():
    """Files written before this change must keep rendering."""
    payload = json.loads(to_json(_report_with_two_pages()))
    del payload["action_plan"]

    assert Report.model_validate(payload).action_plan == []
```

If `tests/unit/reportmodel_test.py` has no two-page helper, build the report the way the file's existing tests do and name the helper `_report_with_two_pages` so these tests read as written.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/reportmodel_test.py -k "plan or top_actions" -v`
Expected: FAIL — `Report` has no attribute `action_plan`, or the plan is empty.

- [ ] **Step 3: Populate the plan in `build_report`**

In `analysis/reportmodel.py`, inside `build_report`, after the `PageBlock` list is built and before the `Report(...)` construction:

```python
    from analysis.priority import rank_actions
    from report.glossary import load_glossary

    glossary = load_glossary()
    plan = rank_actions(page_blocks, glossary=glossary,
                        thresholds=settings.thresholds)
```

(`page_blocks` is whatever local list of `PageBlock` the function already assembles — use the existing name.)

Pass `action_plan=plan` to `Report(...)`, and derive the summary's actions from
it, keeping the model's own three when the plan is empty:

```python
        summary=Summary(
            problem=summary.problem,
            key_finding=summary.key_finding,
            top_actions=(
                [f"{a.title} ({a.page})" for a in plan[:MAX_TOP_ACTIONS]]
                if plan else list(summary.top_actions)
            ),
        ),
```

`MAX_TOP_ACTIONS = 3` is defined in `analysis/__main__.py`; define the same
constant locally in `reportmodel.py` rather than importing it — importing the
CLI module from the model layer inverts the dependency.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/unit/reportmodel_test.py tests/unit/priority_test.py -v`
Expected: PASS. Some existing tests assert `top_actions` content; if one now fails because the plan reorders them, update that assertion to the new order **only after confirming the new order is the ranked one** — do not weaken the assertion to make it pass.

- [ ] **Step 5: Commit**

```bash
git add analysis/reportmodel.py tests/unit/reportmodel_test.py
git commit -m "Draw the executive actions from the ranked plan"
```

---

### Task 4: Plain-language fields on the model contract

**Files:**
- Modify: `analysis/llm.py` (`LlmFinding`, `LlmRecommendation`, `JSON_INSTRUCTION`)
- Modify: `rag/prompt.py` (`SYSTEM_PROMPT` — audience instruction)
- Modify: `analysis/findings.py` (carry the fields into `FindingModel`/`RecommendationModel` inputs)
- Modify: `analysis/reportmodel.py` (`FindingModel.consequence`, `RecommendationModel.why_it_matters`)
- Test: `tests/unit/analysis_llm_test.py`, `tests/unit/findings_test.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `LlmFinding.consequence: str`, `LlmRecommendation.why_it_matters: str`, `FindingModel.consequence: str`, `RecommendationModel.why_it_matters: str` — all defaulting to `""`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/analysis_llm_test.py`:

```python
def test_a_finding_carries_a_plain_language_consequence():
    payload = dict(VALID_PAGE)
    payload["findings"] = [dict(VALID_PAGE["findings"][0])]
    payload["findings"][0]["consequence"] = (
        "Visitors stare at an empty hero while the video downloads."
    )
    client = make_client([json.dumps(payload)])

    result = client.analyze_page(a_prompt())

    assert result.findings[0].consequence.startswith("Visitors stare")


def test_a_recommendation_carries_why_it_matters():
    payload = dict(VALID_PAGE)
    payload["recommendations"] = [dict(VALID_PAGE["recommendations"][0])]
    payload["recommendations"][0]["why_it_matters"] = (
        "The page becomes usable sooner on a phone."
    )
    client = make_client([json.dumps(payload)])

    result = client.analyze_page(a_prompt())

    assert result.recommendations[0].why_it_matters.startswith("The page becomes")


def test_the_new_fields_are_optional():
    """A model that omits them degrades to today's output, not to an error."""
    result = make_client([json.dumps(VALID_PAGE)]).analyze_page(a_prompt())

    assert result.findings[0].consequence == ""
    assert result.recommendations[0].why_it_matters == ""


def test_the_output_contract_asks_for_plain_language():
    from analysis.llm import JSON_INSTRUCTION

    assert "consequence" in JSON_INSTRUCTION
    assert "why_it_matters" in JSON_INSTRUCTION
```

Append to `tests/unit/findings_test.py`:

```python
def test_plain_language_fields_reach_the_page_analysis():
    run, symptoms, chunks = _setup()
    result_model = LlmPageAnalysis.model_validate({
        "summary": "s",
        "findings": [{"title": "t", "detail": "d",
                      "consequence": "Taps do nothing for two seconds."}],
        "impacts": [],
        "recommendations": [
            {"title": "Real", "rationale": "r", "playbook_source": "images.md",
             "playbook_section": "Serve modern formats",
             "why_it_matters": "The page reacts to taps sooner."},
        ],
    })

    result = analyze_page([run], hits=[a_hit("images.md")], symptoms=symptoms,
                          client=FakeClient(result_model), chunks=chunks)

    assert result.findings[0].consequence == "Taps do nothing for two seconds."
    assert result.recommendations[0].why_it_matters == "The page reacts to taps sooner."
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/analysis_llm_test.py tests/unit/findings_test.py -k "consequence or why_it_matters or plain_language" -v`
Expected: FAIL — the models drop unknown fields, so the assertions see `""` or raise `AttributeError`.

- [ ] **Step 3: Add the fields**

`analysis/llm.py`:

```python
class LlmFinding(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=2000)
    #: What a visitor to this page actually experiences. Plain language, no
    #: numbers — the reader of this line is not a performance engineer.
    consequence: str = Field(default="", max_length=500)
    evidence: List[str] = Field(default_factory=list, max_length=20)
    symptom_codes: List[str] = Field(default_factory=list, max_length=20)
```

```python
class LlmRecommendation(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(default="", max_length=2000)
    #: Why this is worth doing, stated for someone deciding whether to fund it.
    why_it_matters: str = Field(default="", max_length=500)
    playbook_source: str = Field(min_length=1, max_length=200)
    playbook_section: str = Field(default="", max_length=200)
```

Extend `JSON_INSTRUCTION`'s shape block to:

```
  "findings": [
    {"title": "...", "detail": "...",
     "consequence": "what a visitor to this page actually experiences, in one
      plain sentence, no numbers and no metric names",
     "evidence": ["metric=value"], "symptom_codes": ["lcp_fail"]}
  ],
  ...
  "recommendations": [
    {"title": "...", "rationale": "...",
     "why_it_matters": "one plain sentence for someone deciding whether to fund
      this work, no numbers",
     "playbook_source": "<the source name of a playbook shown above>",
     "playbook_section": "<the heading you used>"}
  ]
```

Mirror the same two fields on `FindingModel` and `RecommendationModel` in
`analysis/reportmodel.py`, both `= Field(default="", max_length=500)`, and pass
them through wherever those models are constructed from the LLM result in
`analysis/findings.py` (search for `FindingModel(` and `RecommendationModel(`
and add the argument at each site; the rule-based builder passes `""`).

- [ ] **Step 4: Add the audience instruction to the prompt**

In `rag/prompt.py`, add to `SYSTEM_PROMPT`'s numbered rules:

```
6. Your reader is a project manager with no performance background. Write \
consequences and rationales in plain language: what a visitor to this page \
experiences, and what the business gains from the fix. Never restate metric \
values in those fields — the report prints the numbers itself, with their \
targets, right beside your words.
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/unit/analysis_llm_test.py tests/unit/findings_test.py tests/unit/rag_test.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add analysis/llm.py analysis/findings.py analysis/reportmodel.py rag/prompt.py tests/
git commit -m "Ask the model for consequences a non-specialist can read"
```

---

### Task 5: The at-a-glance table and the renderers

**Files:**
- Modify: `report/render_html.py` (expose the glossary to the template context)
- Modify: `report/template/report.html.j2` (new `page.at-a-glance`, `plan`, `page.detail` blocks; formatted values)
- Modify: `report/template/report.md.j2` (same sections in Markdown)
- Modify: `report/template/style.css` (`.shot img` print height cap)
- Test: `tests/unit/render_html_test.py`, `tests/unit/render_md_test.py`

**Interfaces:**
- Consumes: `report.glossary.load_glossary` (Task 1); `Report.action_plan` (Tasks 2–3); `FindingModel.consequence`, `RecommendationModel.why_it_matters` (Task 4).
- Produces: rendered sections `plan`, `page.at-a-glance`, `page.detail`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/render_html_test.py` (build the report with whatever helper the module already uses):

```python
def test_the_at_a_glance_table_states_target_and_verdict():
    html = render_html(_report())

    assert 'data-section="page.at-a-glance"' in html
    assert "Target" in html and "What it means" in html


def test_the_plan_section_is_rendered_in_rank_order():
    html = render_html(_report())

    assert 'data-section="plan"' in html
    body = html.split('data-section="plan"', 1)[1]
    assert body.index("1.") < body.index("2.")


def test_technical_detail_is_grouped_behind_its_own_heading():
    html = render_html(_report())

    assert 'data-section="page.detail"' in html


def test_no_raw_float_reaches_the_page():
    """2438.5999999940395 was printed verbatim in the trend table."""
    import re

    html = render_html(_report())

    assert not re.search(r"\d+\.\d{4,}", html)


def test_the_first_campaign_says_so_instead_of_listing_new_rows():
    html = render_html(_report_without_history())

    assert 'data-section="page.trend"' in html
    assert "no history to compare yet" in html.lower()
```

Append the equivalents to `tests/unit/render_md_test.py` for the Markdown mirror:

```python
def test_markdown_carries_the_plan_and_the_glance_table():
    md = render_markdown(_report())

    assert "## What to do first" in md
    assert "| Metric | Measured | Target | Verdict | What it means |" in md


def test_markdown_prints_no_raw_floats():
    import re

    assert not re.search(r"\d+\.\d{4,}", render_markdown(_report()))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/render_html_test.py tests/unit/render_md_test.py -v`
Expected: FAIL — the sections do not exist and raw floats are present.

- [ ] **Step 3: Expose the glossary to the templates**

In `report/render_html.py`, where the Jinja context is assembled, add:

```python
    from report.glossary import load_glossary

    glossary = load_glossary()
```

and pass `glossary=glossary` into `template.render(...)`, plus a small helper
built in Python rather than in Jinja (templates should not compute):

```python
def glance_rows(page, glossary, thresholds):
    """The at-a-glance rows: measurement, target, verdict, plain meaning."""
    rows = []
    for metric in ("lcp_ms", "cls", "inp_ms", "tbt_ms", "ttfb_ms"):
        value = page.metrics.get(metric)
        target = page.targets.get(metric)
        if target is None:
            target = glossary.target_for(metric, thresholds)
        rows.append({
            "label": glossary.label(metric),
            "value": glossary.format_value(metric, value),
            "target": glossary.format_value(metric, target),
            "verdict": glossary.context(metric, value, target),
            "plain": glossary.gloss(metric),
        })
    return rows
```

Pass the computed rows per page into the context. Do the same in
`report/render_md.py` so both renderers share one source of truth — if that
means lifting `glance_rows` into `report/glossary.py` and importing it in both,
do that rather than duplicating it.

- [ ] **Step 4: Restructure the HTML template**

In `report/template/report.html.j2`:

1. After the `summary` section, add the plan:

```jinja
<section data-section="plan" class="sheet">
  <h2>What to do first</h2>
  {% if report.action_plan %}
  <ol class="plan">
    {% for action in report.action_plan %}
    <li>
      <strong>{{ action.title }}</strong> <span class="muted">({{ action.page }})</span>
      {% if action.why_it_matters %}<p>{{ action.why_it_matters }}</p>{% endif %}
      <p class="muted">Effort: {{ action.effort }}{% if action.projected %} ·
        expected: {{ action.projected }}{% endif %}</p>
    </li>
    {% endfor %}
  </ol>
  {% else %}
  <p class="muted">No actions were identified for this campaign.</p>
  {% endif %}
</section>
```

2. Inside the page block, immediately after `page.header`, add:

```jinja
  <table data-section="page.at-a-glance" class="metrics glance">
    <thead><tr><th>Metric</th><th>Measured</th><th>Target</th>
      <th>Verdict</th><th>What it means</th></tr></thead>
    <tbody>
      {% for row in page.glance %}
      <tr><td>{{ row.label }}</td><td>{{ row.value }}</td><td>{{ row.target }}</td>
        <td>{{ row.verdict }}</td><td>{{ row.plain }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
```

3. Wrap the existing `page.cwv-dashboard`, `page.resources`, `page.lcp-breakdown`, `page.trend` and `page.projections` blocks — unchanged, in that order — in:

```jinja
  <div data-section="page.detail" class="block detail">
    <h3>Technical detail</h3>
    ... the five existing blocks, moved verbatim ...
  </div>
```

4. In `page.findings`, render each finding's consequence under its detail:

```jinja
      {% if finding.consequence %}<p class="consequence">{{ finding.consequence }}</p>{% endif %}
```

5. In `page.trend`, replace the row loop's empty case so a page with no history renders one line:

```jinja
    {% if page.trends and page.trends|selectattr('direction', 'ne', 'new')|list %}
      ... existing table ...
    {% else %}
      <p class="muted">First campaign — no history to compare yet.</p>
    {% endif %}
```

6. Everywhere a metric value is printed in a table cell, route it through the glossary formatter: `{{ glossary.format_value('lcp_ms', row.metrics.lcp_ms) }}`.

Mirror all six changes in `report/template/report.md.j2`, using the Markdown
headings `## What to do first` and `#### Technical detail`, and the glance table
header `| Metric | Measured | Target | Verdict | What it means |`.

- [ ] **Step 5: Cap the screenshot height in print**

In `report/template/style.css`, replace the `.shot img` rule:

```css
.shot img {
  max-width: 100%;
  max-height: 9cm;          /* a capture must not own a page in the PDF */
  height: auto;
  object-fit: cover;
  object-position: top;
  border: 1px solid var(--rule);
}
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/unit/render_html_test.py tests/unit/render_md_test.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add report/ tests/unit/render_html_test.py tests/unit/render_md_test.py
git commit -m "Lead with plain language, put the evidence behind it"
```

---

### Task 6: Smaller captures, and the new skeleton baseline

**Files:**
- Modify: `config/settings.yaml` (`report.appendix` sizes)
- Modify: `report/skeleton.baseline.json` (regenerated, committed as its own diff)
- Test: `tests/unit/config_test.py`, `tests/unit/skeleton_test.py`

**Interfaces:**
- Consumes: the template from Task 5.
- Produces: the committed baseline matching the new section list.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/config_test.py`:

```python
def test_appendix_captures_are_sized_to_share_a_page():
    appendix = cl.load_settings().report.appendix

    assert appendix.screenshot_width_px == 480
    assert appendix.screenshot_max_height_px == 400
```

Append to `tests/unit/skeleton_test.py` (follow the module's existing style for rendering and reading the baseline):

```python
def test_the_baseline_leads_with_plain_language():
    from report.skeleton import load_baseline

    sections = load_baseline()["sections"]

    assert sections.index("plan") < sections.index("page[]")
    assert sections.index("page.at-a-glance") < sections.index("page.findings")
    assert sections.index("page.findings") < sections.index("page.detail")


def test_the_rendered_report_matches_the_committed_baseline():
    from report.skeleton import diff_against_baseline, sections_of

    assert diff_against_baseline(sections_of(render_html(_report()))) == []
```

Use whatever the module's real function names are — read `report/skeleton.py`
before writing this test and match its API exactly rather than assuming these
names.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/config_test.py tests/unit/skeleton_test.py -v`
Expected: FAIL — old sizes, and the baseline has neither `plan` nor `page.at-a-glance`.

- [ ] **Step 3: Shrink the captures**

In `config/settings.yaml`:

```yaml
  appendix:
    top_requests: 15
    # Captures share a page with their request table rather than owning one:
    # six full-page screenshots at the old size made a three-page campaign a
    # forty-page PDF. The full-resolution PNG stays under data/raw and is
    # referenced by path beneath each thumbnail.
    screenshot_width_px: 480
    screenshot_max_height_px: 400
```

Update the same defaults on `AppendixConfig` in `config/load.py`.

- [ ] **Step 4: Regenerate the baseline**

Run: `python -m cli report --update-baseline` against a rendered report (use the
campaign in `data/reports/` if one is present, otherwise render the fixture the
skeleton tests use). Inspect the diff: it must show exactly `plan`,
`page.at-a-glance` and `page.detail` added and the five detail blocks moved —
nothing removed. If anything else changed, the template moved a block by
accident; fix the template, not the baseline.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/unit/skeleton_test.py tests/unit/config_test.py -v`
Expected: PASS.

- [ ] **Step 6: Commit the baseline on its own**

```bash
git add config/settings.yaml config/load.py tests/unit/config_test.py tests/unit/skeleton_test.py
git commit -m "Size appendix captures to share a page"
git add report/skeleton.baseline.json
git commit -m "Regenerate the skeleton baseline for the reader-first order"
```

---

### Task 7: End to end, and the documentation

**Files:**
- Test: `tests/integration/report_pipeline_test.py`
- Modify: `README.md`, `docs/PROJECT_SPEC.md`

- [ ] **Step 1: Write the failing integration test**

```python
def test_the_rendered_report_reads_plain_first(tmp_path):
    """Plan before pages, glance before findings, evidence last."""
    report = _pipeline_report()          # module's existing end-to-end helper
    html = render_html(report)

    assert html.index('data-section="plan"') < html.index('data-section="page"')
    assert (html.index('data-section="page.at-a-glance"')
            < html.index('data-section="page.findings"')
            < html.index('data-section="page.detail"'))


def test_the_plan_is_ordered_and_drives_the_summary(tmp_path):
    report = _pipeline_report()

    scores = [a.rank for a in report.action_plan]
    assert scores == sorted(scores)
    if report.action_plan:
        assert report.summary.top_actions[0].startswith(report.action_plan[0].title)
```

- [ ] **Step 2: Run it to verify it fails, then passes after Tasks 1–6**

Run: `python -m pytest tests/integration/report_pipeline_test.py -v`

- [ ] **Step 3: Run the entire suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass. Existing render tests that assert the old ordering
must be updated to the new ordering — read each failure and confirm it is the
intended restructuring before changing an assertion.

- [ ] **Step 4: Regenerate the live report and check the page count**

Run:

```bash
python -m cli analyze
python -m cli report
```

Confirm: `mode=llm`, the PDF is materially shorter than the previous 40 pages,
and the first two pages read as plain language. Record the actual page count in
the commit message — the claim in the spec is "roughly 12", and the real number
is what belongs in the history.

- [ ] **Step 5: Update the documentation**

In `README.md`, under "The report", describe the reader-first order: what to do
first, then per page a plain at-a-glance table with targets, then findings with
consequences, then technical detail, then the appendix. Note that the glossary
at `data/knowledge/glossary.yaml` is where the plain wording lives and that
editing it changes every future report.

In `docs/PROJECT_SPEC.md` §6, update the fixed-skeleton section list to the new
order, since that section is the specification of the skeleton this baseline now
enforces.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/report_pipeline_test.py README.md docs/PROJECT_SPEC.md
git commit -m "Document the reader-first report"
```

---

## Self-Review

- **Spec coverage:** §1 glossary → Task 1. §2 ranker + `action_plan` → Tasks 2–3. §3 contract fields → Task 4. §4 at-a-glance → Task 5. §5 skeleton → Tasks 5–6 (template) and 6 (baseline). §6 images → Task 6. §7 backwards compatibility → Task 3 (defaulted `action_plan` test) and Task 4 (defaulted string fields). §8 testing → tests in every task.
- **Placeholder scan:** every code step carries real code. Three steps say "use the module's existing helper" (`_report()`, `_report_with_two_pages`, `_pipeline_report`) and one says "read `report/skeleton.py` and match its API" — these are instructions to reuse a specific existing thing, not deferred decisions.
- **Type consistency:** `PlannedAction` fields are spelled identically in Tasks 2, 3, 5. `glossary.format_value(metric, value)`, `.label`, `.gloss`, `.context`, `.target_for`, `.has` match between Task 1's implementation and their uses in Tasks 2 and 5. `rank_actions(pages, *, glossary, thresholds)` matches between Task 2 and Task 3. `consequence` / `why_it_matters` are the same names on the LLM models, the report models and the templates.
