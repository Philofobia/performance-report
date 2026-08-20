"""The committed plain-language layer over the report's metrics.

Every sentence here is data, not model output. That is the point: a model asked
to explain "total blocking time" writes it differently every run, and this
project's headline promise is that two campaigns produce comparable documents.
The model still writes the narrative that ties findings together; it never
writes what a metric *is*.

Rounding lives here too. The raw values are floats measured to microsecond
precision, and every renderer printed them verbatim — the trend table shipped
``2438.5999999940395`` to the reader. One formatter, shared by all three
renderers, fixes that at the only point they have in common.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "glossary.yaml"

#: What a value with no measurement renders as, everywhere.
MISSING = "—"

#: The metrics the at-a-glance table shows, in the order a reader meets them:
#: what they waited for, what moved, how it responded, how long it was frozen,
#: and how much of that was the server.
GLANCE_METRICS = ("lcp_ms", "cls", "inp_ms", "tbt_ms", "ttfb_ms")


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
        raise GlossaryError(
            f"Could not read the glossary at {source}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise GlossaryError(
            f"The glossary at {source} is not valid YAML: {exc}"
        ) from exc

    entries: Dict[str, MetricGloss] = {}
    for metric, body in raw.items():
        if not isinstance(body, dict):
            raise GlossaryError(
                f"Glossary entry {metric!r} must be a mapping of "
                "label/unit/round/target_key/plain."
            )
        entries[str(metric)] = MetricGloss(
            label=str(body.get("label", metric)),
            unit=str(body.get("unit", "") or ""),
            round=str(body.get("round", "raw")),
            target_key=body.get("target_key") or None,
            plain=str(body.get("plain", "")),
        )
    return Glossary(entries)


def glance_rows(page: Any, glossary: Glossary,
                thresholds: Any) -> List[Dict[str, str]]:
    """The at-a-glance rows: measurement, target, verdict, plain meaning.

    Built here rather than in a template so the HTML and Markdown renderers
    cannot drift apart — the two documents are meant to be the same report in
    two shapes.
    """
    rows: List[Dict[str, str]] = []
    for metric in GLANCE_METRICS:
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
