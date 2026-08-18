# Phase 7C — Local Manual-Entry Web Form Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve a loopback-only HTML form that produces exactly the run JSON `python -m cli ingest manual` produces, from the same validation gate.

**Architecture:** A `webui/` package in three layers. `form.py` is a pure field table — it maps HTML input names to canonical `Run` locations, coerces strings to numbers, and maps Pydantic error locations back to field names; it validates nothing. `app.py` is a WSGI callable that routes, reads the request, calls `ingest.manual.build_manual_run`, writes the JSON and redirects. `__main__.py` binds a `wsgiref` server to loopback and refuses anything else.

**Tech Stack:** Python 3.11+, stdlib (`wsgiref`, `urllib.parse`, `http`), Jinja2 3.1.6 and Pydantic 2.13.4 (both already direct dependencies). No new packages. Tests: pytest 9.1.1; one `e2e`-marked Playwright test.

## Global Constraints

- **No new dependency.** `requirements.txt` must not gain a line. This project pins only what it imports and CI runs `pip-audit` over the result.
- **One validation gate.** No range check, no unit rule, no URL rule may be written in `webui/`. Everything goes through `ingest.manual.build_manual_run` → `normalize.schema.Run.model_validate`. Client-side `min`/`max` are read out of `model_fields` at render time, never typed into the template.
- **Blank is not zero.** An empty numeric input is `None`; `"0"` is `0.0`.
- **Loopback only.** `--host` accepts `127.0.0.1`, `localhost`, `::1`. Anything else exits `2` before a socket is created.
- **Autoescape on**, declared as `select_autoescape(enabled_extensions=("html", "j2"), default_for_string=True, default=True)`. `select_autoescape()` with its defaults does **not** escape a file named `*.html.j2` — it matches on the final extension.
- Offline suite must stay browser-free and port-free: `pytest -m "not e2e"` binds nothing.
- Every file starts with a module docstring explaining *why*, matching the house style in `report/render_html.py`.
- Commit after every task, message in the imperative, ending with the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.

---

### Task 1: The field contract (`webui/form.py`)

**Files:**
- Create: `webui/__init__.py`, `webui/form.py`
- Test: `tests/unit/webui_form_test.py`

**Interfaces:**
- Consumes: `normalize.schema.{CwpMetrics, LighthouseScores, NetworkMetrics, Condition}`
- Produces:
  - `FormField` dataclass: `.name .label .group .kind .cast .loc .model .attr .unit .required`
  - `FIELDS: tuple[FormField, ...]`, `GROUPS: tuple[str, ...]`
  - `grouped() -> list[tuple[str, list[FormField]]]`
  - `constraints(field: FormField) -> dict[str, Any]`
  - `parse(form: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, str]]` → `(build_manual_run kwargs, field errors)`
  - `field_errors(exc: ValidationError) -> dict[str, str]`
  - `manual_error_field(message: str) -> str`
  - `RUNNER = "manual-webui"`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/webui_form_test.py`:

```python
"""Unit tests for webui/form.py — the form's field contract.

This module is the only place in the UI that touches raw strings, so it is the
only place that can get coercion wrong. The property that matters is the one
the run listing already depends on: a blank field is an absent measurement, not
a zero. A UI that turned an untouched LCP box into `0` would report the slowest
page in the campaign as the fastest.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from normalize.schema import CwpMetrics, LighthouseScores, Run
from webui import form


def submitted(**overrides):
    """A complete, valid submission; override one key per test."""
    values = {f.name: "" for f in form.FIELDS}
    values.update({
        "project": "storefront",
        "page": "homepage",
        "page_url": "https://example.com/",
        "device": "mid-mobile",
        "network": "slow-4g",
        "problem": "Homepage LCP spikes on 3G",
        "lcp_ms": "6200",
        "cls": "0.42",
    })
    values.update(overrides)
    return values


# --- coercion ---------------------------------------------------------------


def test_blank_numeric_is_absent_not_zero():
    kwargs, errors = form.parse(submitted(inp_ms=""))
    assert errors == {}
    assert kwargs["cwp"]["inp_ms"] is None


def test_a_measured_zero_survives_as_zero():
    kwargs, _ = form.parse(submitted(cls="0"))
    assert kwargs["cwp"]["cls"] == 0.0


def test_surrounding_whitespace_is_stripped():
    kwargs, _ = form.parse(submitted(lcp_ms="  6200 "))
    assert kwargs["cwp"]["lcp_ms"] == 6200.0


def test_non_numeric_input_is_a_field_error_naming_the_label():
    _, errors = form.parse(submitted(lcp_ms="abc"))
    assert "lcp_ms" in errors
    assert "LCP" in errors["lcp_ms"]


def test_whole_number_fields_reject_a_decimal_with_a_specific_message():
    _, errors = form.parse(submitted(runs="2.5"))
    assert "whole number" in errors["runs"]


def test_keywords_split_on_commas_and_blank_means_auto_derive():
    kwargs, _ = form.parse(submitted(keywords="lcp, hero , "))
    assert kwargs["keywords"] == ["lcp", "hero"]
    assert form.parse(submitted(keywords=""))[0]["keywords"] is None


def test_blank_context_fields_fall_through_to_the_cli_defaults():
    """An omitted key lets build_manual_run apply its own default."""
    kwargs, _ = form.parse(submitted(project="", device=""))
    assert "project" not in kwargs
    assert "device" not in kwargs


def test_parse_output_is_accepted_by_build_manual_run():
    from ingest.manual import build_manual_run
    kwargs, errors = form.parse(submitted())
    assert errors == {}
    run = build_manual_run(**kwargs)
    assert isinstance(run, Run)
    assert run.meta.runner == form.RUNNER


# --- schema-derived constraints ---------------------------------------------


def test_constraints_are_read_from_the_schema_not_hardcoded():
    cls_field = next(f for f in form.FIELDS if f.name == "cls")
    assert form.constraints(cls_field) == {"min": 0, "max": 1, "step": "any"}


def test_lighthouse_constraints_track_the_schema():
    perf = next(f for f in form.FIELDS if f.name == "performance")
    limits = form.constraints(perf)
    assert limits["max"] == LighthouseScores.model_fields["performance"].metadata[-1].le


def test_fields_without_a_schema_constraint_return_nothing():
    problem = next(f for f in form.FIELDS if f.name == "problem")
    assert form.constraints(problem) == {}


# --- error mapping ----------------------------------------------------------


def test_nested_pydantic_loc_maps_back_to_the_form_field():
    with pytest.raises(ValidationError) as exc:
        CwpMetrics(cls=1.5)
    errors = form.field_errors(exc.value)
    assert "cls" in errors


def test_page_and_project_url_errors_do_not_collide():
    """Both report loc `(..., 'url')`; only the full path tells them apart."""
    names = {f.loc: f.name for f in form.FIELDS}
    assert names[("page", "url")] == "page_url"
    assert names[("project", "url")] == "project_url"


def test_an_unrecognised_loc_becomes_a_form_level_error():
    with pytest.raises(ValidationError) as exc:
        Run.model_validate({"run_id": "r"})
    assert form.FORM_LEVEL in form.field_errors(exc.value)


def test_manual_error_field_distinguishes_the_two_urls():
    assert form.manual_error_field("Invalid page url: bad scheme") == "page_url"
    assert form.manual_error_field("Invalid project url: bad scheme") == "project_url"


# --- grouping ---------------------------------------------------------------


def test_every_field_belongs_to_a_declared_group():
    assert {f.group for f in form.FIELDS} == set(form.GROUPS)
    assert sum(len(fields) for _, fields in form.grouped()) == len(form.FIELDS)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/webui_form_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webui'`

- [ ] **Step 3: Write the implementation**

Create `webui/__init__.py`:

```python
"""Local manual-entry web form (PROJECT_SPEC §10 Phase 7C).

A browser front door to `ingest.manual`, not a second ingestion path. Design:
`docs/superpowers/specs/2026-08-18-phase-7c-web-ui-design.md`.
"""
```

Create `webui/form.py`:

```python
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

    FormField("problem", "What's wrong", "What's wrong", kind="textarea",
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

#: Context fields that survive a save, and the metric fields that must not.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/webui_form_test.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add webui/__init__.py webui/form.py tests/unit/webui_form_test.py
git commit -m "Map the form's fields onto the canonical run schema

Constraints and error locations are read from the Pydantic model rather
than restated, so a range rule stays in one place. A blank numeric input
is None, never 0 - the run listing's em-dash rule, upstream of it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Render the form (`webui/app.py` GET, template, stylesheet)

**Files:**
- Create: `webui/app.py`, `webui/template/form.html.j2`, `webui/template/style.css`
- Test: `tests/integration/webui_app_test.py`

**Interfaces:**
- Consumes: `webui.form.{FIELDS, grouped, constraints, STICKY}`
- Produces:
  - `Application(*, output_dir, devices=(), networks=())` — a WSGI callable
  - `Application.__call__(environ, start_response) -> list[bytes]`
  - `MAX_BODY_BYTES = 65536`, `FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"`
  - Routes: `GET /`, `POST /runs`, `GET /static/style.css`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/webui_app_test.py`:

```python
"""Integration tests for the manual-entry WSGI application.

The app is a plain callable over two dicts, so these tests build an `environ`
and call it — no socket, no port, no browser. That is the whole reason the
WSGI shape was chosen over BaseHTTPRequestHandler; see the design doc §3.1.
"""
from __future__ import annotations

import io
import json
from urllib.parse import urlencode

import pytest

from webui import form
from webui.app import FORM_CONTENT_TYPE, MAX_BODY_BYTES, Application


@pytest.fixture()
def app(tmp_path):
    return Application(
        output_dir=tmp_path / "processed",
        devices=["mid-mobile", "desktop"],
        networks=["slow-4g", "fast-3g"],
    )


def call(app, method="GET", path="/", query="", body="",
         content_type=FORM_CONTENT_TYPE, content_length=None):
    """Drive the WSGI callable directly. Returns (status, headers, body)."""
    raw = body.encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(raw)) if content_length is None else content_length,
        "wsgi.input": io.BytesIO(raw),
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = app(environ, start_response)
    return captured["status"], captured["headers"], b"".join(chunks).decode("utf-8")


def submission(**overrides):
    values = {
        "project": "storefront",
        "page": "homepage",
        "page_url": "https://example.com/",
        "device": "mid-mobile",
        "network": "slow-4g",
        "problem": "Homepage LCP spikes to 6s on 3G",
        "lcp_ms": "6200",
        "cls": "0.42",
    }
    values.update(overrides)
    return urlencode(values)


# --- GET / ------------------------------------------------------------------


def test_get_renders_an_input_for_every_declared_field(app):
    status, headers, body = call(app)
    assert status.startswith("200")
    assert headers["Content-Type"].startswith("text/html")
    for field in form.FIELDS:
        assert f'name="{field.name}"' in body


def test_range_hints_come_from_the_schema(app):
    """`max="1"` on CLS must be the schema's number, not the template's."""
    _, _, body = call(app)
    assert 'name="cls"' in body
    assert 'max="1"' in body
    assert 'max="100"' in body          # Lighthouse scores


def test_device_and_network_render_the_configured_presets(app):
    _, _, body = call(app)
    assert '<option value="mid-mobile"' in body
    assert '<option value="fast-3g"' in body


def test_the_saved_banner_names_the_run_and_prefills_the_context(app):
    _, _, body = call(app, query=urlencode({
        "saved": "run_20260818_101500_ab12",
        "page": "homepage",
        "page_url": "https://example.com/",
    }))
    assert "run_20260818_101500_ab12" in body
    assert 'value="homepage"' in body


def test_the_stylesheet_is_served_as_css(app):
    status, headers, body = call(app, path="/static/style.css")
    assert status.startswith("200")
    assert headers["Content-Type"].startswith("text/css")
    assert "--paper" in body


# --- routing ----------------------------------------------------------------


def test_an_unknown_path_is_404(app):
    status, _, _ = call(app, path="/nope")
    assert status.startswith("404")


def test_posting_to_the_form_path_is_405(app):
    status, headers, _ = call(app, method="POST", path="/", body=submission())
    assert status.startswith("405")
    assert headers.get("Allow") == "GET"


def test_getting_the_runs_path_is_405(app):
    status, headers, _ = call(app, path="/runs")
    assert status.startswith("405")
    assert headers.get("Allow") == "POST"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/webui_app_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webui.app'`

- [ ] **Step 3: Write the implementation**

Create `webui/app.py` (GET routes only; POST arrives in Task 3 — for now `_post_run` returns `501`, replaced next task):

```python
"""WSGI application for the local manual-entry form.

A plain ``app(environ, start_response)`` callable, which is what makes it
testable without a socket: a test builds two dictionaries and calls it. The
alternative — a ``BaseHTTPRequestHandler`` subclass — puts socket I/O inside
the unit under test and forces either a live port or hand-mocked file objects
into an otherwise offline suite.

The application computes nothing and validates nothing. It routes, reads the
request, calls ``ingest.manual.build_manual_run``, writes the JSON, and
redirects.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape

from webui import form

TEMPLATE_DIR = Path(__file__).parent / "template"
FORM_TEMPLATE = "form.html.j2"
STYLESHEET = "style.css"

#: A body larger than this is refused unread. `Problem.description` is capped
#: at 10 000 characters by the schema, so no legitimate submission is near it.
MAX_BODY_BYTES = 64 * 1024

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

StartResponse = Callable[[str, List[Tuple[str, str]]], Any]


class Application:
    """The manual-entry form, as a WSGI application."""

    def __init__(self, *, output_dir: Path, devices: Sequence[str] = (),
                 networks: Sequence[str] = ()) -> None:
        self.output_dir = Path(output_dir)
        self.devices = list(devices)
        self.networks = list(networks)
        # `select_autoescape()` matches on the *final* extension, so a file
        # named `form.html.j2` would not be escaped by its defaults. The form
        # echoes user prose back into HTML on both the error and success
        # paths; this is the boundary that has to escape it.
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(
                enabled_extensions=("html", "j2"), default_for_string=True, default=True
            ),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._env.globals["constraints"] = form.constraints
        # Read once at startup and served from memory: there is no
        # request-path-to-filesystem-path translation here, and therefore no
        # traversal to defend against.
        self._stylesheet = (TEMPLATE_DIR / STYLESHEET).read_text(encoding="utf-8")

    # --- WSGI ---------------------------------------------------------------

    def __call__(self, environ: Mapping[str, Any],
                 start_response: StartResponse) -> List[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"

        if path == "/static/style.css":
            if method != "GET":
                return self._method_not_allowed(start_response, "GET")
            return self._respond(start_response, "200 OK",
                                 self._stylesheet.encode("utf-8"),
                                 "text/css; charset=utf-8")

        if path == "/":
            if method != "GET":
                return self._method_not_allowed(start_response, "GET")
            return self._get_form(environ, start_response)

        if path == "/runs":
            if method != "POST":
                return self._method_not_allowed(start_response, "POST")
            return self._post_run(environ, start_response)

        return self._respond(start_response, "404 Not Found", b"Not found",
                             "text/plain; charset=utf-8")

    # --- routes -------------------------------------------------------------

    def _get_form(self, environ: Mapping[str, Any],
                  start_response: StartResponse) -> List[bytes]:
        query = {k: v[0] for k, v in
                 parse_qs(environ.get("QUERY_STRING", "")).items()}
        saved = query.pop("saved", "")
        page = self._render(query, saved=saved)
        return self._respond(start_response, "200 OK", page.encode("utf-8"),
                             "text/html; charset=utf-8")

    def _post_run(self, environ: Mapping[str, Any],
                  start_response: StartResponse) -> List[bytes]:
        return self._respond(start_response, "501 Not Implemented", b"",
                             "text/plain; charset=utf-8")

    # --- helpers ------------------------------------------------------------

    def _render(self, values: Mapping[str, str], *,
                errors: Mapping[str, str] = {}, saved: str = "") -> str:
        return self._env.get_template(FORM_TEMPLATE).render(
            groups=form.grouped(),
            values=values,
            errors=errors,
            saved=saved,
            form_level=errors.get(form.FORM_LEVEL, ""),
            devices=self.devices,
            networks=self.networks,
        )

    def _respond(self, start_response: StartResponse, status: str, body: bytes,
                 content_type: str,
                 extra: Iterable[Tuple[str, str]] = ()) -> List[bytes]:
        headers = [("Content-Type", content_type),
                   ("Content-Length", str(len(body)))]
        headers.extend(extra)
        start_response(status, headers)
        return [body]

    def _method_not_allowed(self, start_response: StartResponse,
                            allowed: str) -> List[bytes]:
        return self._respond(start_response, "405 Method Not Allowed",
                             b"Method not allowed", "text/plain; charset=utf-8",
                             [("Allow", allowed)])
```

Create `webui/template/form.html.j2`:

```jinja
{# The manual-entry form.

   No JavaScript: the browser submits it, and native `type="number"` with the
   schema-derived min/max gives client-side feedback for free. A script would
   only duplicate rules the server re-checks anyway — the second copy this
   design exists to avoid. #}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Record a run — performance RAG</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
<main>
  <header class="masthead">
    <h1>Record a run</h1>
    <p class="lede">Hand-entered measurements, validated by the same schema the
      browser campaigns use, written to <code>data/processed</code>.</p>
  </header>

  {% if saved %}
  <p class="banner banner--ok" role="status">
    Saved <code>{{ saved }}</code>. Run <code>python -m cli analyze</code> to
    include it in the next report.
  </p>
  {% endif %}

  {% if form_level %}
  <p class="banner banner--bad" role="alert">{{ form_level }}</p>
  {% endif %}

  <form method="post" action="/runs">
    {% for group, fields in groups %}
    <fieldset>
      <legend>{{ group }}</legend>
      <div class="grid">
        {% for field in fields %}
        {% set limits = constraints(field) %}
        <div class="field{% if errors.get(field.name) %} field--bad{% endif %}">
          <label for="f-{{ field.name }}">
            {{ field.label }}
            {% if field.unit %}<span class="unit">{{ field.unit }}</span>{% endif %}
            {% if field.required %}<span class="req" aria-hidden="true">required</span>{% endif %}
          </label>

          {% if field.kind == 'textarea' %}
          <textarea id="f-{{ field.name }}" name="{{ field.name }}" rows="4"
            >{{ values.get(field.name, '') }}</textarea>

          {% elif field.kind == 'select' %}
          <select id="f-{{ field.name }}" name="{{ field.name }}">
            {% for option in (devices if field.name == 'device' else networks) %}
            <option value="{{ option }}"
              {% if values.get(field.name) == option %}selected{% endif %}>{{ option }}</option>
            {% endfor %}
          </select>

          {% else %}
          <input id="f-{{ field.name }}" name="{{ field.name }}"
                 type="{{ 'number' if field.kind == 'number' else 'text' }}"
                 value="{{ values.get(field.name, '') }}"
                 {% if field.required %}required{% endif %}
                 {% if 'min' in limits %}min="{{ limits['min'] }}"{% endif %}
                 {% if 'max' in limits %}max="{{ limits['max'] }}"{% endif %}
                 {% if 'step' in limits %}step="{{ limits['step'] }}"{% endif %}>
          {% endif %}

          {% if errors.get(field.name) %}
          <p class="err">{{ errors[field.name] }}</p>
          {% endif %}
        </div>
        {% endfor %}
      </div>
    </fieldset>
    {% endfor %}

    <button type="submit">Save run</button>
  </form>
</main>
</body>
</html>
```

Create `webui/template/style.css`:

```css
/* Data-entry sheet for the manual-run form.
 *
 * Shares the report's palette tokens by value; report/palette.py remains the
 * source of truth, exactly as report/template/style.css already declares. The
 * report stylesheet itself is not reused: it is a print sheet built around
 * @page, an A4 measure and tabular figures for a document nobody types into.
 */

:root {
  --paper: #f2f4f3;
  --paper-raised: #ffffff;
  --ink: #1a1d21;
  --muted: #6b7280;
  --rule: #d8dce1;
  --accent: #2f5d8f;
  --pass: #1a7f4b;
  --fail: #b3261e;

  --sans: Corbel, "Segoe UI", Candara, "Trebuchet MS", sans-serif;
  --mono: Consolas, "Cascadia Mono", "DejaVu Sans Mono", monospace;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 2rem 1.25rem 4rem;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  line-height: 1.5;
}

main { max-width: 60rem; margin: 0 auto; }

.masthead { border-bottom: 2px solid var(--ink); padding-bottom: 0.75rem; }
.masthead h1 { margin: 0; font-size: 1.6rem; letter-spacing: -0.01em; }
.lede { margin: 0.4rem 0 0; color: var(--muted); max-width: 46ch; }

code { font-family: var(--mono); font-size: 0.9em; }

.banner {
  margin: 1.25rem 0 0;
  padding: 0.7rem 0.9rem;
  border-left: 3px solid var(--accent);
  background: var(--paper-raised);
}
.banner--ok { border-left-color: var(--pass); }
.banner--bad { border-left-color: var(--fail); }

fieldset {
  margin: 1.75rem 0 0;
  padding: 1rem 1.1rem 1.25rem;
  border: 1px solid var(--rule);
  background: var(--paper-raised);
}

legend {
  padding: 0 0.5rem;
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
  gap: 0.9rem 1.1rem;
}

.field { display: flex; flex-direction: column; gap: 0.3rem; }
.field:has(textarea) { grid-column: 1 / -1; }

label { font-size: 0.85rem; font-weight: 600; }
.unit { color: var(--muted); font-weight: 400; }
.req {
  margin-left: 0.35rem;
  font-size: 0.7rem;
  font-weight: 400;
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

input, select, textarea {
  padding: 0.45rem 0.55rem;
  border: 1px solid var(--rule);
  border-radius: 2px;
  background: var(--paper-raised);
  color: inherit;
  font: inherit;
  font-variant-numeric: tabular-nums;
}

input:focus-visible, select:focus-visible, textarea:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

textarea { font-family: inherit; resize: vertical; }

.field--bad input, .field--bad select, .field--bad textarea {
  border-color: var(--fail);
}
.err { margin: 0; color: var(--fail); font-size: 0.8rem; }

button {
  margin-top: 1.5rem;
  padding: 0.6rem 1.4rem;
  border: 0;
  border-radius: 2px;
  background: var(--ink);
  color: var(--paper-raised);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
button:hover { background: var(--accent); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/webui_app_test.py -v`
Expected: PASS. (The `POST /runs` tests do not exist yet; only the GET and routing tests above are present.)

- [ ] **Step 5: Commit**

```bash
git add webui/app.py webui/template tests/integration/webui_app_test.py
git commit -m "Render the manual-entry form from the field contract

The range hints in the markup are generated from the Pydantic model, so
the browser's own validation cannot drift from the schema's. Autoescape
is declared for the .j2 extension: select_autoescape's defaults match on
the final extension and would leave form.html.j2 unescaped.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Accept a submission (`POST /runs`, happy path)

**Files:**
- Modify: `webui/app.py` — replace `_post_run`
- Test: `tests/integration/webui_app_test.py` — append

**Interfaces:**
- Consumes: `ingest.manual.build_manual_run`, `webui.form.{parse, field_errors, manual_error_field, STICKY}`
- Produces: `303 See Other` to `/?saved=<run_id>&<sticky context>`; a file at `<output_dir>/<run_id>.json`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/webui_app_test.py`:

```python
# --- POST /runs, happy path -------------------------------------------------


def test_a_valid_submission_writes_one_run_json(app, tmp_path):
    status, headers, _ = call(app, method="POST", path="/runs", body=submission())
    assert status.startswith("303")

    written = list((tmp_path / "processed").glob("*.json"))
    assert len(written) == 1

    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["metrics"]["cwp"]["lcp_ms"] == 6200
    assert payload["meta"]["source"] == "manual"
    assert payload["meta"]["runner"] == form.RUNNER


def test_the_redirect_names_the_run_and_carries_the_context_forward(app):
    _, headers, _ = call(app, method="POST", path="/runs", body=submission())
    location = headers["Location"]
    assert location.startswith("/?")
    assert "saved=run_" in location
    assert "page=homepage" in location
    assert "lcp_ms" not in location          # metrics must not be sticky


def test_the_written_file_is_named_for_the_run_id(app, tmp_path):
    _, headers, _ = call(app, method="POST", path="/runs", body=submission())
    run_id = headers["Location"].split("saved=")[1].split("&")[0]
    assert (tmp_path / "processed" / f"{run_id}.json").exists()


def test_two_submissions_do_not_collide(app, tmp_path):
    call(app, method="POST", path="/runs", body=submission())
    call(app, method="POST", path="/runs", body=submission())
    assert len(list((tmp_path / "processed").glob("*.json"))) == 2


def test_the_ui_and_the_cli_produce_the_same_run(app, tmp_path, monkeypatch):
    """The parity test.

    The UI is a front door to manual ingestion, not a second ingestion path.
    Identical inputs must produce identical payloads — everything but the
    fields that are *supposed* to differ: the id, the timestamp, and the
    runner that records which door was used.
    """
    from ingest.manual import main as manual_main

    cli_output = tmp_path / "cli.json"
    assert manual_main([
        "--project", "storefront", "--page", "homepage",
        "--page-url", "https://example.com/",
        "--device", "mid-mobile", "--network", "slow-4g",
        "--problem", "Homepage LCP spikes to 6s on 3G",
        "--lcp-ms", "6200", "--cls", "0.42",
        "--output", str(cli_output),
    ]) == 0

    call(app, method="POST", path="/runs", body=submission())
    ui_output = next((tmp_path / "processed").glob("*.json"))

    volatile = {"run_id", "created_at", "runner"}

    def stable(payload):
        payload = json.loads(json.dumps(payload))
        payload.pop("run_id")
        for key in ("created_at", "runner"):
            payload["meta"].pop(key)
        return payload

    assert stable(json.loads(ui_output.read_text(encoding="utf-8"))) == \
           stable(json.loads(cli_output.read_text(encoding="utf-8")))
    assert volatile  # documents what was excluded and why


def test_the_output_directory_is_created_on_demand(tmp_path):
    app = Application(output_dir=tmp_path / "deep" / "processed")
    status, _, _ = call(app, method="POST", path="/runs", body=submission())
    assert status.startswith("303")
    assert (tmp_path / "deep" / "processed").is_dir()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/webui_app_test.py -v -k "valid_submission or parity or redirect_names"`
Expected: FAIL — status is `501 Not Implemented`, not `303`.

- [ ] **Step 3: Write the implementation**

In `webui/app.py`, add the imports:

```python
import json
from urllib.parse import parse_qs, urlencode

from pydantic import ValidationError

from ingest.manual import ManualValidationError, build_manual_run
```

Replace `_post_run` in full:

```python
    def _post_run(self, environ: Mapping[str, Any],
                  start_response: StartResponse) -> List[bytes]:
        submitted = self._read_form(environ)

        kwargs, errors = form.parse(submitted)
        run = None
        if not errors:
            try:
                run = build_manual_run(**kwargs)
            except ManualValidationError as exc:
                errors = {form.manual_error_field(str(exc)): str(exc)}
            except ValidationError as exc:
                errors = form.field_errors(exc)

        if errors:
            page = self._render(submitted, errors=errors)
            return self._respond(start_response, "400 Bad Request",
                                 page.encode("utf-8"), "text/html; charset=utf-8")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / f"{run.run_id}.json"
        destination.write_text(
            json.dumps(run.model_dump(mode="json"), indent=2), encoding="utf-8"
        )

        # 303 rather than a rendered result: a reload re-issues the GET, so it
        # cannot write a second run. The context rides along in the query so
        # the next entry — almost always the same page under a different
        # condition — starts filled, while every metric starts empty.
        query = {"saved": run.run_id}
        query.update({name: submitted.get(name, "") for name in form.STICKY})
        return self._respond(start_response, "303 See Other", b"",
                             "text/plain; charset=utf-8",
                             [("Location", "/?" + urlencode(query))])

    def _read_form(self, environ: Mapping[str, Any]) -> Dict[str, str]:
        """Read and decode the submitted form body."""
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
```

Note `_read_form` is deliberately unguarded here; Task 4 adds the caps and returns them as responses.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/webui_app_test.py -v`
Expected: PASS, all tests including the parity test.

- [ ] **Step 5: Commit**

```bash
git add webui/app.py tests/integration/webui_app_test.py
git commit -m "Write a submitted run through the manual ingestion gate

Answers 303 rather than rendering the result, so a reload cannot write a
second run, and carries the page context forward while leaving every
metric blank - inherited metrics would be a data-integrity bug wearing a
convenience feature's clothes.

The parity test pins the claim that matters: same inputs through the CLI
and through the form produce the same payload.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Refuse what should be refused (error paths)

**Files:**
- Modify: `webui/app.py` — guard `_post_run`
- Test: `tests/integration/webui_app_test.py` — append

**Interfaces:**
- Consumes: `MAX_BODY_BYTES`, `FORM_CONTENT_TYPE` from Task 2
- Produces: `400` (re-rendered form, values retained), `411`, `413`, `415`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/webui_app_test.py`:

```python
# --- POST /runs, refusals ---------------------------------------------------


def test_an_out_of_range_value_is_rejected_by_the_schema_not_the_ui(app, tmp_path):
    status, _, body = call(app, method="POST", path="/runs",
                           body=submission(cls="1.5"))
    assert status.startswith("400")
    assert "cls" in body
    assert not list((tmp_path / "processed").glob("*.json"))


def test_a_rejected_submission_keeps_every_value_the_user_typed(app):
    _, _, body = call(app, method="POST", path="/runs",
                      body=submission(cls="1.5", lcp_ms="6200"))
    assert 'value="6200"' in body
    assert 'value="1.5"' in body
    assert "Homepage LCP spikes to 6s on 3G" in body


def test_a_missing_page_url_names_the_field_it_is_missing_from(app):
    status, _, body = call(app, method="POST", path="/runs",
                           body=submission(page_url=""))
    assert status.startswith("400")
    assert "page url" in body.lower()


def test_a_non_https_url_is_refused_by_the_ssrf_gate(app, tmp_path):
    status, _, body = call(app, method="POST", path="/runs",
                           body=submission(page_url="http://169.254.169.254/"))
    assert status.startswith("400")
    assert not list((tmp_path / "processed").glob("*.json"))


def test_a_non_numeric_metric_is_a_field_error_not_a_traceback(app):
    status, _, body = call(app, method="POST", path="/runs",
                           body=submission(lcp_ms="soon"))
    assert status.startswith("400")
    assert "must be a number" in body


def test_an_oversized_body_is_refused_unread(app, tmp_path):
    status, _, _ = call(app, method="POST", path="/runs",
                        body="problem=" + "x" * MAX_BODY_BYTES)
    assert status.startswith("413")
    assert not list((tmp_path / "processed").glob("*.json"))


def test_a_json_content_type_is_refused(app):
    status, _, _ = call(app, method="POST", path="/runs",
                        body=submission(), content_type="application/json")
    assert status.startswith("415")


def test_a_missing_content_length_is_refused(app):
    status, _, _ = call(app, method="POST", path="/runs", body=submission(),
                        content_length="")
    assert status.startswith("411")


def test_a_malformed_content_length_is_refused(app):
    status, _, _ = call(app, method="POST", path="/runs", body=submission(),
                        content_length="not-a-number")
    assert status.startswith("400")


def test_submitted_prose_is_escaped_on_the_way_back_out(app):
    """The error path echoes user text into HTML; it must not echo markup."""
    _, _, body = call(app, method="POST", path="/runs",
                      body=submission(cls="1.5", problem="<script>alert(1)</script>"))
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_the_success_banner_escapes_what_it_reflects(app):
    _, _, body = call(app, query=urlencode({"saved": "<script>alert(1)</script>"}))
    assert "<script>alert(1)</script>" not in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/webui_app_test.py -v -k "oversized or json_content_type or content_length"`
Expected: FAIL — `413`/`415`/`411` are not produced; the oversize case raises or writes a file.

- [ ] **Step 3: Write the implementation**

In `webui/app.py`, replace the first line of `_post_run` with a guard, and rewrite `_read_form` to report rather than assume:

```python
    def _post_run(self, environ: Mapping[str, Any],
                  start_response: StartResponse) -> List[bytes]:
        refusal = self._refuse_request(environ)
        if refusal is not None:
            status, message = refusal
            return self._respond(start_response, status,
                                 message.encode("utf-8"),
                                 "text/plain; charset=utf-8")

        submitted = self._read_form(environ)
        # ... unchanged from Task 3 ...
```

Add the guard and adjust the reader:

```python
    def _refuse_request(self, environ: Mapping[str, Any]):
        """Reject a request that must not reach the parser. ``None`` to proceed.

        The size cap is checked against the declared length and the body is
        never read, so an oversized submission costs nothing to refuse.
        """
        content_type = environ.get("CONTENT_TYPE", "").split(";")[0].strip()
        if content_type != FORM_CONTENT_TYPE:
            return "415 Unsupported Media Type", f"Expected {FORM_CONTENT_TYPE}"

        declared = environ.get("CONTENT_LENGTH", "")
        if declared in ("", None):
            return "411 Length Required", "Content-Length required"
        try:
            length = int(declared)
        except ValueError:
            return "400 Bad Request", "Malformed Content-Length"
        if length < 0:
            return "400 Bad Request", "Malformed Content-Length"
        if length > MAX_BODY_BYTES:
            return "413 Payload Too Large", (
                f"Request body over {MAX_BODY_BYTES} bytes"
            )
        return None

    def _read_form(self, environ: Mapping[str, Any]) -> Dict[str, str]:
        """Read and decode the submitted form body, capped at MAX_BODY_BYTES."""
        length = min(int(environ.get("CONTENT_LENGTH") or 0), MAX_BODY_BYTES)
        raw = environ["wsgi.input"].read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/webui_app_test.py tests/unit/webui_form_test.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add webui/app.py tests/integration/webui_app_test.py
git commit -m "Refuse oversized, mistyped and out-of-range submissions

A rejected form comes back with everything still in it: losing a filled
page to one bad digit is the fastest way to send someone back to the
command line. The size cap is checked against the declared length, so an
oversized body is refused without being read.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Serve it, on loopback only (`webui/__main__.py`, `cli.py`)

**Files:**
- Create: `webui/__main__.py`
- Modify: `cli.py:28-34` (`COMMANDS`), `cli.py:59-72` (loaders and `_DELEGATES`)
- Test: `tests/unit/webui_main_test.py`, `tests/integration/cli_test.py` (append)

**Interfaces:**
- Consumes: `webui.app.Application`, `config.load.{load_devices, load_networks, load_settings, ConfigError}`
- Produces: `webui.__main__.main(argv=None, *, server_factory=None) -> int`; `LOOPBACK_HOSTS`; the `cli.py` command `ui`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/webui_main_test.py`:

```python
"""Unit tests for the manual-entry server's entry point.

The binding rule is the security control this phase turns on, so it is tested
the way a control should be: by proving the socket is never created, not by
proving a warning was printed. `server_factory` is injected for exactly that —
the offline suite binds no port.
"""
from __future__ import annotations

import pytest

from webui.__main__ import LOOPBACK_HOSTS, main


class RecordingFactory:
    """Stands in for wsgiref's make_server; records that it was called."""

    def __init__(self):
        self.calls = []

    def __call__(self, host, port, app):
        self.calls.append((host, port, app))
        return self

    def serve_forever(self):      # the server the factory returns
        return None

    def server_close(self):
        return None


@pytest.mark.parametrize("host", sorted(LOOPBACK_HOSTS))
def test_loopback_hosts_are_accepted(host, tmp_path):
    factory = RecordingFactory()
    code = main(["--host", host, "--output-dir", str(tmp_path)],
                server_factory=factory)
    assert code == 0
    assert factory.calls[0][0] == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "example.com"])
def test_a_non_loopback_host_is_refused_before_any_socket_exists(host, tmp_path, capsys):
    factory = RecordingFactory()
    code = main(["--host", host, "--output-dir", str(tmp_path)],
                server_factory=factory)
    assert code == 2
    assert factory.calls == []
    assert host in capsys.readouterr().err


def test_the_port_is_passed_through(tmp_path):
    factory = RecordingFactory()
    main(["--port", "8123", "--output-dir", str(tmp_path)], server_factory=factory)
    assert factory.calls[0][1] == 8123


def test_the_default_host_is_loopback(tmp_path):
    factory = RecordingFactory()
    main(["--output-dir", str(tmp_path)], server_factory=factory)
    assert factory.calls[0][0] == "127.0.0.1"


def test_a_keyboard_interrupt_is_a_clean_exit(tmp_path):
    class Interrupting(RecordingFactory):
        def serve_forever(self):
            raise KeyboardInterrupt

    assert main(["--output-dir", str(tmp_path)], server_factory=Interrupting()) == 0
```

Append to `tests/integration/cli_test.py`:

```python
def test_the_facade_exposes_the_manual_entry_ui():
    """`python -m cli ui` reaches webui, and the table advertises it."""
    assert "ui" in cli.COMMANDS
    assert cli._DELEGATES["ui"]() is not None
    assert "ui" in cli.usage()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/webui_main_test.py tests/integration/cli_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webui.__main__'`, and `KeyError: 'ui'`.

- [ ] **Step 3: Write the implementation**

Create `webui/__main__.py`:

```python
"""``python -m webui`` — serve the manual-entry form on loopback.

The binding rule is a refusal, not a warning. An unauthenticated endpoint that
writes files to disk, reachable from the network, is precisely the shape
SECURITY_PLAN.md exists to prevent, and a warning is a control that only works
on the people who were not going to make the mistake. Because there is no
remote reachability, there is nothing to authenticate — which is what makes
"no login" a decision here rather than an omission.

``wsgiref.simple_server`` is single-threaded and explicitly not a production
server. That is an accurate description of what this is.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, List, Optional

from webui.app import Application

#: Everything that resolves to this machine and nothing else.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_OUTPUT_DIR = "data/processed"


def _make_server(host: str, port: int, app: Application):
    from wsgiref.simple_server import make_server
    return make_server(host, port, app)


def _presets() -> tuple:
    """Device and network names, so the form offers what the runner accepts.

    A manual run recorded under an invented condition could never be compared
    against a measured one; it would sit in a bucket of its own forever.
    """
    from config.load import load_devices, load_networks
    devices = [d.name for d in load_devices().devices]
    networks = [n.name for n in load_networks().networks]
    return devices, networks


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m webui",
        description="Serve the local manual-entry form (loopback only).",
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"Loopback address to bind. One of: "
                        f"{', '.join(sorted(LOOPBACK_HOSTS))}.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"Port to listen on (default {DEFAULT_PORT}).")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                   help=f"Where run JSON is written (default {DEFAULT_OUTPUT_DIR}).")
    return p


def main(argv: Optional[List[str]] = None, *,
         server_factory: Optional[Callable] = None) -> int:
    """CLI entry point; returns a process exit code (0 = success)."""
    args = _build_parser().parse_args(argv)

    if args.host not in LOOPBACK_HOSTS:
        print(
            f"error: refusing to bind {args.host!r}. This form has no "
            f"authentication and writes files; it serves loopback only "
            f"({', '.join(sorted(LOOPBACK_HOSTS))}).",
            file=sys.stderr,
        )
        return 2

    try:
        devices, networks = _presets()
    except Exception as exc:  # ConfigError and friends
        print(f"error: could not read the device/network presets: {exc}",
              file=sys.stderr)
        return 2

    app = Application(output_dir=Path(args.output_dir),
                      devices=devices, networks=networks)
    server = (server_factory or _make_server)(args.host, args.port, app)

    print(f"Manual entry form: http://{args.host}:{args.port}/")
    print(f"Runs are written to {args.output_dir}. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
```

In `cli.py`, add to `COMMANDS` after `"list-runs"`:

```python
    "ui": "Serve the local manual-entry form (loopback only)",
```

Add the loader beside the others:

```python
def _ui() -> Delegate:
    from webui.__main__ import main
    return main
```

Add to `_DELEGATES`:

```python
    "ui": _ui,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/webui_main_test.py tests/integration/cli_test.py -v`
Expected: PASS.

Then confirm the offline suite is still clean and still binds nothing:

Run: `pytest -m "not e2e" -q`
Expected: PASS, no new failures.

- [ ] **Step 5: Commit**

```bash
git add webui/__main__.py cli.py tests/unit/webui_main_test.py tests/integration/cli_test.py
git commit -m "Serve the form on loopback, and refuse anything else

A non-loopback --host exits 2 before the socket is created, and the test
proves it by asserting the server factory was never called rather than by
reading a warning. Adds `python -m cli ui` to the facade.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Prove the HTML is submittable, and document it

**Files:**
- Create: `tests/e2e/webui_e2e_test.py`
- Modify: `README.md` (Where the project is, Running it, Roadmap, Testing count), `docs/PROJECT_SPEC.md:541`
- Test: the e2e test is the test

**Interfaces:**
- Consumes: `webui.app.Application`, `wsgiref.simple_server`, Playwright's sync API

- [ ] **Step 1: Write the failing test**

Create `tests/e2e/webui_e2e_test.py`:

```python
"""End-to-end: a real browser fills the real form.

Every other test drives the WSGI callable directly, which proves the handler
is correct and proves nothing about the markup. An unclosed tag, a missing
`name`, a button outside the form — all of them pass the integration suite and
none of them submit. This is the only test that would catch them.
"""
from __future__ import annotations

import json
import threading
from wsgiref.simple_server import make_server

import pytest

from webui.app import Application

pytestmark = pytest.mark.e2e


@pytest.fixture()
def server(tmp_path):
    """The app on an ephemeral loopback port, torn down with the test."""
    app = Application(
        output_dir=tmp_path / "processed",
        devices=["mid-mobile", "desktop"],
        networks=["slow-4g", "fast-3g"],
    )
    httpd = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/", tmp_path / "processed"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_browser_can_fill_and_submit_the_form(server):
    from playwright.sync_api import sync_playwright

    url, output_dir = server

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)

        page.fill("#f-page_url", "https://example.com/")
        page.fill("#f-page", "homepage")
        page.fill("#f-problem", "Hero video delays LCP on 3G")
        page.fill("#f-lcp_ms", "6200")
        page.fill("#f-cls", "0.42")
        page.select_option("#f-device", "mid-mobile")

        page.click("button[type=submit]")
        page.wait_for_selector(".banner--ok")

        banner = page.text_content(".banner--ok")
        browser.close()

    written = list(output_dir.glob("*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["run_id"] in banner
    assert payload["metrics"]["cwp"]["lcp_ms"] == 6200
    assert payload["page"]["name"] == "homepage"


def test_the_browser_blocks_an_out_of_range_value_before_it_is_sent(server):
    """`max` came from the schema; this proves the browser honours it."""
    from playwright.sync_api import sync_playwright

    url, output_dir = server

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)
        page.fill("#f-page_url", "https://example.com/")
        page.fill("#f-cls", "1.5")
        page.click("button[type=submit]")
        valid = page.eval_on_selector("#f-cls", "el => el.checkValidity()")
        browser.close()

    assert valid is False
    assert not list(output_dir.glob("*.json"))
```

- [ ] **Step 2: Run the test to verify it fails, then passes**

Run: `pytest -m e2e tests/e2e/webui_e2e_test.py -v`

If it fails on markup, fix `webui/template/form.html.j2` — that is the defect this task exists to find. If Chromium is missing, run `python -m playwright install chromium` first.
Expected once correct: PASS.

- [ ] **Step 3: Update the documentation**

In `README.md`:

1. In **Where the project is**, add to the working-today list, after the appendix clause:
   `· **a loopback-only web form for manual entry**`
2. Delete the `Optional web UI for manual entry` row from the **Missing** table, leaving only the CI row.
3. In **Running it**, add to the command block:

```bash
python -m cli ui                    # loopback-only form for manual entry
```

4. Add a subsection after **Manual ingestion**:

```markdown
### The manual-entry form

```bash
python -m cli ui                    # http://127.0.0.1:8765/
python -m cli ui --port 9000 --output-dir data/processed
```

A browser front door to the same ingestion path: the form posts to
`build_manual_run`, so every unit and range rule in `normalize/schema.py`
applies exactly as it does on the command line, and the run JSON it writes is
byte-identical to the CLI's apart from the run id, the timestamp and
`meta.runner`. The range limits in the markup are generated from the Pydantic
model rather than typed into the template, so they cannot drift from it.

It **serves loopback only**. A `--host` that is not `127.0.0.1`, `localhost` or
`::1` exits with an error rather than a warning: the form has no
authentication, and it writes files. There is nothing to log in to because
there is nothing remote to reach it.

No JavaScript, no build step, no new dependency.
```

5. In **Roadmap**, set `7c` to `Done` and `7d` to `**Next**`.
6. In **Testing**, update the offline test count to the number `pytest -m "not e2e" -q` actually reports.

In `docs/PROJECT_SPEC.md`, replace line 541:

```markdown
- [x] **7c — Local manual-entry web form.** `webui/` — a stdlib WSGI app over
      `ingest.manual.build_manual_run`, bound to loopback by refusal. Design:
      `docs/superpowers/specs/2026-08-18-phase-7c-web-ui-design.md`.
```

- [ ] **Step 4: Verify the full suite and the real thing**

Run: `pytest -m "not e2e" -q` — expected PASS, and note the count for the README.
Run: `pytest -m e2e tests/e2e/webui_e2e_test.py -q` — expected PASS.
Run: `python -m cli ui --port 8765`, open `http://127.0.0.1:8765/`, submit one run, confirm the file appears in `data/processed`, then Ctrl-C.
Run: `python -m cli ui --host 0.0.0.0` — expected exit 2 with the refusal message.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/webui_e2e_test.py README.md docs/PROJECT_SPEC.md
git commit -m "Prove the form submits in a real browser, and document it

The integration suite proves the handler; only a browser proves the
markup. Also asserts the schema-derived max actually stops a bad CLS at
the client, which is the payoff for generating the limits.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage.** §2 in: `form.py` T1 · `app.py` T2–T4 · `__main__.py` T5 · templates T2 · `cli.py` T5 · docs T6. §3.1 WSGI → T2 (`Application.__call__`). §3.2 one gate + schema-derived hints → T1 (`constraints`, `field_errors`) and T2's `test_range_hints_come_from_the_schema`. §3.3 303 and retained values → T3, T4. §3.4 output path, no SQLite write → T3 (no store import anywhere in `webui/`). §3.5 field set → T1 `FIELDS`, all 25 CLI-equivalent fields, `tbt_ms` excluded to match the CLI, `source`/`runner` fixed. §3.6 no JS → template carries none. §4 tests → T1, T2, T3, T4, T5, T6 covering every listed case. §5.1 loopback refusal → T5. §5.2 body cap, content type, no static mapping, escaping, SSRF → T4 and T2. §6 styling → T2. §7 consequences → T6.

**Placeholders.** None: every step carries the code it needs. The one deliberate stub (`_post_run` returning `501` in T2) is named as a stub and replaced in T3's step 3.

**Type consistency.** `FormField.loc` is `tuple[str, ...]` in T1 and indexed as such by `_BY_LOC` and by T1's collision test. `parse` returns `(kwargs, errors)` in T1 and is unpacked that way in T3. `form.STICKY` is defined in T1 and consumed in T3. `Application(*, output_dir, devices, networks)` is constructed identically in T2's fixture, T3's `test_the_output_directory_is_created_on_demand`, T5's `main`, and T6's server fixture. `server_factory(host, port, app)` matches `_make_server`'s signature and `RecordingFactory.__call__`. `FORM_LEVEL` is defined in T1 and used by T1's test and T2's `_render`.

**One gap found and closed while reviewing:** the spec's §4 lists a `415` case but not `411`; a WSGI request with no `CONTENT_LENGTH` would otherwise read zero bytes and report a confusing "page url is required". T4 now returns `411 Length Required` and tests it.
