"""The form's field contract: HTML input names ↔ canonical ``Run`` locations.

This module validates nothing, and that is the entire point. It converts
submitted strings to numbers and hands them to
``ingest.manual.build_manual_run`` — the same gate ``ingest manual`` uses — so
every range rule keeps living in ``normalize/schema.py`` and nowhere else. A UI
that re-stated "CLS is 0 to 1" would be correct the day it was written and
wrong the day the schema moved, with nothing failing in between.

The same reasoning drives ``constraints()``: the browser's own ``min``/``max``
validation is read out of the Pydantic model at render time rather than typed
into the template, because a hand-typed limit is that second copy again.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pydantic import ValidationError

from normalize.schema import (
    Condition,
    CwpMetrics,
    LighthouseScores,
    NetworkMetrics,
)

#: `meta.runner` for runs entered here, so a run's origin is recoverable from
#: the run itself rather than from whoever remembers producing it.
RUNNER = "manual-webui"

#: Key for an error that belongs to no single field.
FORM_LEVEL = "__form__"


@dataclass(frozen=True)
class FormField:
    """One input. ``loc`` is where Pydantic reports failures for it."""

    name: str
    label: str
    group: str
    kind: str = "number"          # number | text | textarea | select
    cast: str = "float"           # float | int | str
    loc: Tuple[str, ...] = ()
    model: Optional[type] = None  # the model carrying its constraints
    attr: str = ""
    unit: str = ""
    required: bool = False


GROUPS: Tuple[str, ...] = (
    "What page",
    "Under what conditions",
    "What's wrong",
    "Core Web Vitals",
    "Targets",
    "Lighthouse",
    "Network",
)

FIELDS: Tuple[FormField, ...] = (
    FormField("project", "Project", "What page", kind="text", cast="str",
              loc=("project", "name")),
    FormField("project_url", "Project URL", "What page", kind="text", cast="str",
              loc=("project", "url")),
    FormField("page", "Page name", "What page", kind="text", cast="str",
              loc=("page", "name")),
    FormField("page_url", "Page URL", "What page", kind="text", cast="str",
              loc=("page", "url"), required=True),

    FormField("device", "Device", "Under what conditions", kind="select",
              cast="str", loc=("condition", "device")),
    FormField("network", "Network", "Under what conditions", kind="select",
              cast="str", loc=("condition", "network")),
    FormField("cpu_throttle", "CPU throttle", "Under what conditions",
              loc=("condition", "cpu_throttle"), model=Condition,
              attr="cpu_throttle", unit="×"),
    FormField("runs", "Runs", "Under what conditions", cast="int",
              loc=("condition", "runs"), model=Condition, attr="runs"),

    FormField("problem", "Description", "What's wrong", kind="textarea",
              cast="str", loc=("problem", "description")),
    FormField("keywords", "Keywords", "What's wrong", kind="text", cast="str",
              loc=("problem", "keywords")),

    FormField("lcp_ms", "LCP", "Core Web Vitals", loc=("metrics", "cwp", "lcp_ms"),
              model=CwpMetrics, attr="lcp_ms", unit="ms"),
    FormField("cls", "CLS", "Core Web Vitals", loc=("metrics", "cwp", "cls"),
              model=CwpMetrics, attr="cls"),
    FormField("inp_ms", "INP", "Core Web Vitals", loc=("metrics", "cwp", "inp_ms"),
              model=CwpMetrics, attr="inp_ms", unit="ms"),
    FormField("fcp_ms", "FCP", "Core Web Vitals", loc=("metrics", "cwp", "fcp_ms"),
              model=CwpMetrics, attr="fcp_ms", unit="ms"),
    FormField("ttfb_ms", "TTFB", "Core Web Vitals", loc=("metrics", "cwp", "ttfb_ms"),
              model=CwpMetrics, attr="ttfb_ms", unit="ms"),

    FormField("target_lcp_ms", "Target LCP", "Targets",
              loc=("metrics", "cwp", "target_lcp_ms"), model=CwpMetrics,
              attr="target_lcp_ms", unit="ms"),
    FormField("target_cls", "Target CLS", "Targets",
              loc=("metrics", "cwp", "target_cls"), model=CwpMetrics,
              attr="target_cls"),
    FormField("target_inp_ms", "Target INP", "Targets",
              loc=("metrics", "cwp", "target_inp_ms"), model=CwpMetrics,
              attr="target_inp_ms", unit="ms"),

    FormField("performance", "Performance", "Lighthouse", cast="int",
              loc=("metrics", "lighthouse", "performance"),
              model=LighthouseScores, attr="performance"),
    FormField("accessibility", "Accessibility", "Lighthouse", cast="int",
              loc=("metrics", "lighthouse", "accessibility"),
              model=LighthouseScores, attr="accessibility"),
    FormField("best_practices", "Best practices", "Lighthouse", cast="int",
              loc=("metrics", "lighthouse", "best_practices"),
              model=LighthouseScores, attr="best_practices"),
    FormField("seo", "SEO", "Lighthouse", cast="int",
              loc=("metrics", "lighthouse", "seo"),
              model=LighthouseScores, attr="seo"),

    FormField("total_transfer_kb", "Total transfer", "Network",
              loc=("metrics", "network", "total_transfer_kb"),
              model=NetworkMetrics, attr="total_transfer_kb", unit="kB"),
    FormField("request_count", "Requests", "Network", cast="int",
              loc=("metrics", "network", "request_count"),
              model=NetworkMetrics, attr="request_count"),
    FormField("render_blocking_css", "Render-blocking CSS", "Network", cast="int",
              loc=("metrics", "network", "render_blocking_css"),
              model=NetworkMetrics, attr="render_blocking_css"),
)

_BY_LOC: Dict[Tuple[str, ...], str] = {f.loc: f.name for f in FIELDS if f.loc}

#: Context fields that survive a save. Every metric is deliberately absent:
#: a stale measurement inherited into a new run is a data-integrity bug
#: wearing a convenience feature's clothes.
STICKY = ("project", "project_url", "page", "page_url", "device", "network")


def grouped() -> List[Tuple[str, List[FormField]]]:
    """Fields in render order, as the page reads them."""
    return [(g, [f for f in FIELDS if f.group == g]) for g in GROUPS]


def constraints(field: FormField) -> Dict[str, Any]:
    """The field's HTML validation attributes, read from its Pydantic model.

    Duck-typed over the constraint metadata rather than importing
    ``annotated_types``: this project pins what it imports, and reading two
    attributes does not justify a new direct dependency.
    """
    if field.model is None or not field.attr:
        return {}
    info = field.model.model_fields[field.attr]
    limits: Dict[str, Any] = {}
    for meta in info.metadata:
        ge = getattr(meta, "ge", None)
        le = getattr(meta, "le", None)
        if ge is not None:
            limits["min"] = ge
        if le is not None:
            limits["max"] = le
    if limits and field.cast == "float":
        limits["step"] = "any"
    return limits


def _coerce(field: FormField, raw: str) -> Tuple[Any, Optional[str]]:
    """One value, string → typed. Returns ``(value, error message)``."""
    if raw == "":
        return None, None
    try:
        return (int(raw) if field.cast == "int" else float(raw)), None
    except ValueError:
        kind = "a whole number" if field.cast == "int" else "a number"
        return None, f"{field.label} must be {kind}."


def parse(form: Mapping[str, str]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Submitted strings → ``build_manual_run`` kwargs, plus coercion errors.

    Blank stays blank: an untouched box is ``None``, never ``0``.
    """
    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for field in FIELDS:
        raw = (form.get(field.name) or "").strip()
        if field.cast == "str":
            values[field.name] = raw
            continue
        value, message = _coerce(field, raw)
        if message:
            errors[field.name] = message
        values[field.name] = value

    return _to_kwargs(values), errors


def _group_values(values: Mapping[str, Any], model: type) -> Dict[str, Any]:
    return {f.attr: values[f.name] for f in FIELDS if f.model is model and f.attr}


def _to_kwargs(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Shape the coerced values as ``build_manual_run`` expects them.

    A blank context field is *omitted* rather than passed as ``""``, so the
    CLI's own defaults apply and the two entry points cannot disagree about
    what "unspecified" means.
    """
    keywords = [k.strip() for k in str(values["keywords"]).split(",") if k.strip()]

    kwargs: Dict[str, Any] = {
        "problem": values["problem"],
        "keywords": keywords or None,
        "page_url": values["page_url"],
        "source": "manual",
        "runner": RUNNER,
        "cwp": _group_values(values, CwpMetrics),
        "lighthouse": _group_values(values, LighthouseScores),
        "network_metrics": _group_values(values, NetworkMetrics),
    }

    for name in ("project", "project_url", "page", "device", "network"):
        if values[name]:
            kwargs[name] = values[name]
    for name in ("cpu_throttle", "runs"):
        if values[name] is not None:
            kwargs[name] = values[name]

    return kwargs


def field_errors(exc: ValidationError) -> Dict[str, str]:
    """Pydantic error locations → form field names.

    Mapped on the *full* location path: ``page.url`` and ``project.url`` both
    end in ``url``, and matching on the last element alone would report a bad
    page URL against the project field.
    """
    mapped: Dict[str, str] = {}
    for error in exc.errors():
        loc = tuple(str(part) for part in error["loc"])
        name = _BY_LOC.get(loc, FORM_LEVEL)
        mapped.setdefault(name, error["msg"])
    return mapped


def manual_error_field(message: str) -> str:
    """Attribute a ``ManualValidationError`` to the URL field that caused it."""
    return "project_url" if "project url" in message.lower() else "page_url"
