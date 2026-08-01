"""Manual ingestion: CLI + validation -> a normalized canonical ``Run``.

Accepts a free-text problem description and/or metric values (CWV, Lighthouse
scores, network basics) and converges them to :class:`normalize.schema.Run`.

Validation is always forced through the Pydantic canonical schema: inputs are
placed into the canonical payload and ``Run.model_validate`` is the single gate,
so every unit/range rule in ``normalize/schema.py`` (e.g. ``lcp_ms >= 0``,
``cls`` in 0..1, Lighthouse 0..100) applies unchanged to manual entries.

URLs (project/page) are checked with ``url_safety.validate_url(..., resolve=False)``
per SECURITY_PLAN.md §2.2/§2.4; manual ingestion never navigates, so DNS
resolution is skipped but scheme/userinfo/raw-IP guards still hold.

Usage::

    python -m ingest.manual --problem "Homepage LCP spikes to 6s" \\
        --lcp-ms 6200 --cls 0.42 --inp-ms 480 --lh-performance 54 \\
        --page homepage --page-url https://example.com/ --output run.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from normalize import url_safety
from normalize.schema import Run

DEFAULT_RUNNER = "manual-cli"

# Tokens we look for in free-text to auto-derive problem keywords (PROJECT_SPEC §4.2).
KNOWN_KEYWORD_TOKENS = (
    "lcp",
    "cls",
    "inp",
    "fcp",
    "ttfb",
    "lighthouse",
    "bundle",
    "image",
    "font",
    "javascript",
    "js",
    "css",
    "video",
    "hero",
    "third-party",
    "third party",
    "seo",
    "network",
    "mobile",
    "desktop",
)


class ManualValidationError(ValueError):
    """User-facing error for invalid manual ingestion input."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"


def _safe_url(url: Optional[str], field: str) -> str:
    """Validate a user-supplied URL (SSRF guards, no DNS resolution) and return it."""
    if not url:
        raise ManualValidationError(f"{field} is required")
    try:
        return url_safety.validate_url(url, resolve=False)
    except url_safety.UnSafeURLError as exc:
        raise ManualValidationError(f"Invalid {field}: {exc}") from exc


def derive_keywords(problem: Optional[str]) -> List[str]:
    """Auto-derive a small, deduped keyword list from free-text problem text."""
    if not problem:
        return []
    lowered = problem.lower()
    found: List[str] = []
    for token in KNOWN_KEYWORD_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            found.append(token)
    return found


def build_manual_run(
    problem: Optional[str] = None,
    *,
    keywords: Optional[List[str]] = None,
    project: str = "storefront",
    project_url: Optional[str] = None,
    page: str = "manual",
    page_url: Optional[str] = None,
    device: str = "mid-mobile",
    network: str = "slow-4g",
    cpu_throttle: float = 1.0,
    runs: int = 1,
    source: str = "manual",
    runner: str = DEFAULT_RUNNER,
    cwp: Optional[Dict] = None,
    lighthouse: Optional[Dict] = None,
    network_metrics: Optional[Dict] = None,
) -> Run:
    """Build and validate a canonical ``Run`` from manual input.

    Raises :class:`ManualValidationError` for bad URLs and lets Pydantic's
    :class:`~pydantic.ValidationError` propagate for any bad unit/range.
    """
    if source not in ("manual", "mixed"):
        raise ManualValidationError(f"source must be 'manual' or 'mixed', got {source!r}")

    safe_page_url = _safe_url(page_url, "page url")
    safe_project_url = _safe_url(project_url or safe_page_url, "project url")

    payload = {
        "run_id": _new_run_id(),
        "project": {"name": project or "project", "url": safe_project_url},
        "page": {"name": page or "page", "url": safe_page_url},
        "condition": {
            "device": device,
            "network": network,
            "cpu_throttle": cpu_throttle,
            "runs": runs,
        },
        "meta": {
            "created_at": _now_iso(),
            "source": source,
            "runner": runner,
        },
        "problem": {
            "description": problem or "",
            "keywords": keywords if keywords is not None else derive_keywords(problem),
        },
        "metrics": {
            "cwp": cwp or {},
            "lighthouse": lighthouse or {},
            "network": network_metrics or {},
        },
    }
    # Single validation gate — every canonical rule applies.
    return Run.model_validate(payload)

def _num(value: Optional[str]) -> Optional[float]:
    """argparse type: convert a CLI string to float, or None for empty input."""
    if value in (None, ""):
        return None
    return float(value)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest-manual",
        description="Manual performance ingestion -> normalized canonical Run.",
    )
    p.add_argument("--problem", help="Free-text problem description.")
    p.add_argument(
        "--keywords",
        help="Comma-separated keywords (overrides auto-derivation).",
        default=None,
    )
    p.add_argument("--lcp-ms", type=_num, help="Largest Contentful Paint (ms).")
    p.add_argument("--cls", type=_num, help="Cumulative Layout Shift (0..1).")
    p.add_argument("--inp-ms", type=_num, help="Interaction to Next Paint (ms).")
    p.add_argument("--fcp-ms", type=_num, help="First Contentful Paint (ms).")
    p.add_argument("--ttfb-ms", type=_num, help="Time to First Byte (ms).")
    p.add_argument("--target-lcp-ms", type=_num, help="Target LCP (ms).")
    p.add_argument("--target-cls", type=_num, help="Target CLS (0..1).")
    p.add_argument("--target-inp-ms", type=_num, help="Target INP (ms).")
    p.add_argument("--bundle-kb", type=_num,
                   help="Alias for --total-transfer-kb (bundle/transfer size in KB).")

    p.add_argument("--lh-performance", type=_num)
    p.add_argument("--lh-accessibility", type=_num)
    p.add_argument("--lh-best-practices", type=_num)
    p.add_argument("--lh-seo", type=_num)

    p.add_argument("--total-transfer-kb", type=_num)
    p.add_argument("--request-count", type=int)
    p.add_argument("--render-blocking-css", type=int)

    p.add_argument("--project", default="storefront")
    p.add_argument("--project-url")
    p.add_argument("--page", default="manual")
    p.add_argument("--page-url", help="Required: URL of the page being described.")
    p.add_argument("--device", default="mid-mobile")
    p.add_argument("--network", default="slow-4g")
    p.add_argument("--cpu-throttle", type=float, default=1.0)
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--source", choices=["manual", "mixed"], default="manual")
    p.add_argument("--runner", default=DEFAULT_RUNNER)
    p.add_argument("--output", help="Write the normalized run JSON to this file.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point; returns a process exit code (0 = success)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    keywords = None
    if args.keywords is not None:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    cwp = {
        "lcp_ms": args.lcp_ms,
        "cls": args.cls,
        "inp_ms": args.inp_ms,
        "fcp_ms": args.fcp_ms,
        "ttfb_ms": args.ttfb_ms,
        "target_lcp_ms": args.target_lcp_ms,
        "target_cls": args.target_cls,
        "target_inp_ms": args.target_inp_ms,
    }
    lighthouse = {
        "performance": args.lh_performance,
        "accessibility": args.lh_accessibility,
        "best_practices": args.lh_best_practices,
        "seo": args.lh_seo,
    }
    transfer = args.total_transfer_kb if args.total_transfer_kb is not None else args.bundle_kb
    network_metrics = {
        "total_transfer_kb": transfer,
        "request_count": args.request_count,
        "render_blocking_css": args.render_blocking_css,
    }

    try:
        run = build_manual_run(
            args.problem,
            keywords=keywords,
            project=args.project,
            project_url=args.project_url,
            page=args.page,
            page_url=args.page_url,
            device=args.device,
            network=args.network,
            cpu_throttle=args.cpu_throttle,
            runs=args.runs,
            source=args.source,
            runner=args.runner,
            cwp=cwp,
            lighthouse=lighthouse,
            network_metrics=network_metrics,
        )
    except ManualValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # Pydantic ValidationError and friends
        print(f"error: invalid input: {exc}", file=sys.stderr)
        return 1

    payload = run.model_dump(mode="json")
    text = json.dumps(payload, indent=2)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(json.dumps({"ok": True, "run_id": run.run_id, "output": str(out)}))
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())

