"""Raw capture persistence: screenshots, HAR, traces (PROJECT_SPEC §9, Phase 3).

Artifacts land under a run-scoped directory so every measurement stays
auditable and the report appendix can find them by ``run_id``::

    <root>/<project>/<page>/<run_id>/{screenshot.png,capture.har,trace.zip}

**Secret hygiene (SECURITY_PLAN.md §2.6).** A HAR records request and response
headers verbatim — `Cookie`, `Set-Cookie` and `Authorization` among them — so a
stored HAR can carry live session tokens. :func:`scrub_har` redacts those
headers, plus query-string parameters that look like credentials, *before* the
file is written to the store. Response bodies are already omitted at capture
time by ``ingest/browser/runner.py``.

Scrubbing is applied on the way in, not on the way out: the store must never
contain an unredacted copy.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

# Headers whose values are credentials/session material.
SENSITIVE_HEADERS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "authentication",
    }
)

# Query parameters that commonly carry credentials in URLs.
SENSITIVE_QUERY_PARAMS = frozenset(
    {"token", "access_token", "id_token", "auth", "key", "api_key", "apikey",
     "password", "secret", "signature", "sig", "session"}
)

ARTIFACT_NAMES = {
    "screenshot": "screenshot.png",
    "har": "capture.har",
    "trace": "trace.zip",
}


class ArtifactError(Exception):
    """User-facing error for artifact persistence failures."""


def run_dir(root: str | Path, project: str, page: str, run_id: str) -> Path:
    """Deterministic per-run artifact directory."""
    return Path(root) / _safe_segment(project) / _safe_segment(page) / _safe_segment(run_id)


def _safe_segment(value: str) -> str:
    """Make a path segment safe: no separators, no traversal, never empty."""
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "-_.") else "_" for ch in (value or "")
    ).strip("._")
    return cleaned or "unnamed"


def _redact_headers(headers: Any) -> List[Dict[str, Any]]:
    """Redact credential-bearing headers in a HAR header list."""
    if not isinstance(headers, list):
        return headers
    out = []
    for header in headers:
        if isinstance(header, dict) and str(header.get("name", "")).lower() in SENSITIVE_HEADERS:
            out.append({**header, "value": REDACTED})
        else:
            out.append(header)
    return out


def redact_url(url: Any) -> Any:
    """Strip credential-looking query parameters and any userinfo from a URL."""
    if not isinstance(url, str) or not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(k.lower() in SENSITIVE_QUERY_PARAMS for k, _ in pairs):
            query = urlencode(
                [
                    (k, REDACTED if k.lower() in SENSITIVE_QUERY_PARAMS else v)
                    for k, v in pairs
                ]
            )

    netloc = parts.netloc
    if "@" in netloc:  # user:pass@host — never keep credentials
        netloc = netloc.rsplit("@", 1)[1]

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def scrub_har(har: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a HAR with credentials removed.

    Redacts sensitive request/response headers, the parsed ``cookies`` arrays,
    and credential-looking query parameters (in both ``request.url`` and the
    ``queryString`` array).
    """
    if not isinstance(har, dict):
        raise ArtifactError("HAR must be a JSON object")

    scrubbed = json.loads(json.dumps(har))  # deep copy; HARs are plain JSON
    entries = scrubbed.get("log", {}).get("entries")
    if not isinstance(entries, list):
        return scrubbed

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for section in ("request", "response"):
            part = entry.get(section)
            if not isinstance(part, dict):
                continue
            if "headers" in part:
                part["headers"] = _redact_headers(part["headers"])
            if isinstance(part.get("cookies"), list):
                part["cookies"] = [
                    {**c, "value": REDACTED} if isinstance(c, dict) else c
                    for c in part["cookies"]
                ]
            if section == "request":
                if "url" in part:
                    part["url"] = redact_url(part["url"])
                if isinstance(part.get("queryString"), list):
                    part["queryString"] = [
                        {**q, "value": REDACTED}
                        if isinstance(q, dict)
                        and str(q.get("name", "")).lower() in SENSITIVE_QUERY_PARAMS
                        else q
                        for q in part["queryString"]
                    ]
    return scrubbed


def scrub_har_file(source: str | Path, target: str | Path) -> Path:
    """Read a HAR, scrub it, and write the redacted copy to ``target``."""
    src, dst = Path(source), Path(target)
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ArtifactError(f"HAR not found: {src}") from exc
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"Malformed HAR {src}: {exc}") from exc

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(scrub_har(raw), indent=2), encoding="utf-8")
    return dst


def store_artifacts(
    root: str | Path,
    run,
    *,
    move: bool = False,
) -> Dict[str, Optional[str]]:
    """Persist a run's captures into the store, scrubbing the HAR on the way in.

    Returns the stored paths keyed by artifact kind. Missing captures are
    reported as ``None`` rather than raising — a run without a trace is valid.
    ``move=True`` relocates the source files instead of copying them.
    """
    target_dir = run_dir(root, run.project.name, run.page.name, run.run_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    stored: Dict[str, Optional[str]] = {}
    for kind, filename in ARTIFACT_NAMES.items():
        source = getattr(run.captures, kind, None)
        if not source:
            stored[kind] = None
            continue
        src = Path(source)
        if not src.exists():
            stored[kind] = None
            continue

        dest = target_dir / filename
        if kind == "har":
            # Never copy an unredacted HAR into the store.
            scrub_har_file(src, dest)
            if move:
                src.unlink(missing_ok=True)
        elif move:
            shutil.move(str(src), str(dest))
        else:
            shutil.copy2(str(src), str(dest))
        stored[kind] = str(dest)

    return stored


def load_artifact_paths(root: str | Path, run) -> Dict[str, Optional[str]]:
    """Locate previously stored artifacts for a run (None when absent)."""
    target_dir = run_dir(root, run.project.name, run.page.name, run.run_id)
    return {
        kind: (str(target_dir / name) if (target_dir / name).exists() else None)
        for kind, name in ARTIFACT_NAMES.items()
    }
