"""Authenticated POST to Grafana's ``/api/ds/query``.

Transport is stdlib ``urllib.request``. This repo has twice deleted pinned
dependencies nothing imported (``reportlab``, ``typer``) and records that it
"pins what it imports"; a JSON POST with a bearer header does not justify
reversing that.

Connection identity comes from ``.env`` via :func:`resolve_grafana_env`, which
follows ``rag.embeddings.resolve_api_key``: a missing value raises a message
naming the *variable*, never echoing a value (SECURITY_PLAN 2.8).
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional
from urllib.error import HTTPError, URLError

from normalize.url_safety import UnSafeURLError, validate_url

DATASOURCE_TYPE = "grafana-clickhouse-datasource"
QUERY_PATH = "/api/ds/query"

#: Every variable this stage needs, with the one-line help its absence prints.
_REQUIRED = {
    "GRAFANA_BASE_URL": "the https URL of your Grafana instance",
    "GRAFANA_TOKEN": "a service-account token with Viewer rights on the datasource",
    "GRAFANA_DATASOURCE_UID": "the ClickHouse datasource uid from the dashboard JSON",
    "GRAFANA_TABLE": "the fully-qualified table the panels read",
}


class GrafanaError(Exception):
    """User-facing error talking to Grafana."""


class MissingGrafanaConfigError(GrafanaError):
    """A required ``.env`` variable is absent or blank."""


@dataclass(frozen=True)
class GrafanaEnv:
    base_url: str
    # repr=False keeps the bearer token out of repr()/str() (and so out of any
    # unhandled traceback, print(), or log call that formats this object) —
    # the same SECURITY_PLAN 2.8 guarantee resolve_api_key gives its callers,
    # just closed at the dataclass's own printable surface. The field stays
    # required and in position: repr=False carries no default, so it does not
    # disturb field ordering.
    token: str = field(repr=False)
    datasource_uid: str
    table: str


def resolve_grafana_env(env: Optional[Mapping[str, str]] = None) -> GrafanaEnv:
    """Read the four connection variables, or explain which one is missing."""
    environ = os.environ if env is None else env
    values: Dict[str, str] = {}
    for name, help_text in _REQUIRED.items():
        value = (environ.get(name) or "").strip()
        if not value:
            raise MissingGrafanaConfigError(
                f"{name} is not set. Copy .env.example to .env and add "
                f"{help_text}. It is read from the environment and must never "
                "be committed."
            )
        values[name] = value

    base_url = values["GRAFANA_BASE_URL"].rstrip("/")
    # The same SSRF guard the browser layer uses, unchanged: https only, no
    # raw IPs, no userinfo, no private ranges. Grafana is a public host, so
    # this feature creates no exception to SECURITY_PLAN 2.2.
    try:
        validate_url(base_url)
    except UnSafeURLError as exc:
        raise GrafanaError(f"GRAFANA_BASE_URL is not usable: {exc}") from exc

    return GrafanaEnv(
        base_url=base_url,
        token=values["GRAFANA_TOKEN"],
        datasource_uid=values["GRAFANA_DATASOURCE_UID"],
        table=values["GRAFANA_TABLE"],
    )


class GrafanaClient:
    """One POST carrying every panel query, so a fetch is a single round trip."""

    def __init__(self, env: GrafanaEnv, *, timeout_s: int = 30,
                 opener: Any = None) -> None:
        self._env = env
        self._timeout = timeout_s
        self._opener = opener or urllib.request.build_opener()

    @property
    def table(self) -> str:
        """The ClickHouse table this client's environment points at.

        Public because the ingestion stage renders SQL against it and must not
        reach through ``_env`` to find it.
        """
        return self._env.table

    def query(self, sql_by_ref: Dict[str, str], *, window: str) -> Dict[str, Any]:
        """Execute every query; return the ``results`` map keyed by refId."""
        payload = {
            "from": f"now-{window}",
            "to": "now",
            "queries": [
                {
                    "refId": ref_id,
                    "datasource": {
                        "type": DATASOURCE_TYPE,
                        "uid": self._env.datasource_uid,
                    },
                    "rawSql": sql,
                    "format": 1,
                }
                for ref_id, sql in sorted(sql_by_ref.items())
            ],
        }
        raw = self._post(json.dumps(payload).encode("utf-8"))
        try:
            document = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GrafanaError(
                "Grafana returned a response that is not JSON. This is usually "
                "a proxy or login page in front of the API rather than Grafana "
                "itself."
            ) from exc
        return document.get("results", {})

    def _post(self, body: bytes) -> bytes:
        """POST with one retry on 5xx and on connection failure.

        A 4xx is never retried: a 401 is a bad token and a 400 is a bad query,
        and retrying either only delays the message that fixes it.
        """
        request = urllib.request.Request(
            self._env.base_url + QUERY_PATH,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._env.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    return response.read()
            except HTTPError as exc:
                if exc.code < 500:
                    raise GrafanaError(
                        f"Grafana refused the request ({exc.code} {exc.reason})."
                        + (" Check GRAFANA_TOKEN and its datasource permissions."
                           if exc.code in (401, 403) else "")
                    ) from exc
                last = exc
            except URLError as exc:
                last = exc
        raise GrafanaError(
            f"Could not reach Grafana at {self._env.base_url} after 2 attempts: "
            f"{last}"
        )
