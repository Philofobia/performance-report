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

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import parse_qs, urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from ingest.manual import ManualValidationError, build_manual_run
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
