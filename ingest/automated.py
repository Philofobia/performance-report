"""Multi-page automated campaign loop.

For every configured page and each (device x network) condition, run the
simulation N times, take the per-metric **median**, and emit ONE normalized
canonical ``Run`` per (page x condition) — per PROJECT_SPEC.md §4.2/§7.

Optional CLI overrides let the user test one device/network/runs across pages
(via ``--device``, ``--network``, ``--runs``) and restrict to named pages via
``--pages name1,name2``.

Automated runs are schema-enforced to carry the CWV trio (LCP/CLS/INP); if a
condition yields no such values the emitted run fails Pydantic validation,
surfacing the problem instead of silently emitting a partial run.

The campaign accepts an injected ``runner`` (an object exposing
``run_condition(url, device, network, ...) -> raw dict``) so tests run against
fakes — no real browser. ``main`` wires a real Playwright ``BrowserRunner``.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from config.load import PageTarget, PageTest, ProjectConfig
from normalize.schema import Run

DEFAULT_RUNNER_NAME = "automated-campaign-1.0"

Numeric = Optional[float]


def median(values: List[Optional[float]]) -> Optional[float]:
    """Median of non-None numeric values, or None if there are none."""
    nums = [v for v in values if isinstance(v, (int, float)) and v is not None]
    return statistics.median(nums) if nums else None


def median_int(values: List[Optional[float]]) -> Optional[int]:
    """Median of count-typed metrics, rounded to a whole number.

    An even run count makes ``statistics.median`` interpolate (e.g. 15.5 for
    [10, 21]), which is not a valid value for an integer field like
    ``request_count``. Round rather than truncate so the count stays closest to
    the observed middle (Python rounds exact .5 ties to even; a half-unit
    difference on a count is immaterial for reporting).
    """
    value = median(values)
    return None if value is None else int(round(value))


def merge_median_metrics(measurements: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Median of each numeric metric across N raw measurement dicts."""
    cwp_runs = [m.get("cwp", {}) for m in measurements]
    lh_runs = [m.get("lighthouse", {}) for m in measurements]
    net_runs = [m.get("network", {}) for m in measurements]
    mt_runs = [m.get("main_thread", {}) for m in measurements]

    cwp_keys = ("lcp_ms", "cls", "inp_ms", "fcp_ms", "ttfb_ms", "tbt_ms")
    lh_keys = ("performance", "accessibility", "best_practices", "seo")
    net_float_keys = ("total_transfer_kb",)
    net_int_keys = ("request_count", "render_blocking_css")
    mt_float_keys = ("script_ms", "layout_ms", "style_ms", "task_ms", "js_heap_kb")
    mt_int_keys = ("dom_nodes", "layout_count", "js_event_listeners", "resource_count")

    def _merge(runs, float_keys=(), int_keys=()):
        merged = {k: median([r.get(k) for r in runs]) for k in float_keys}
        merged.update({k: median_int([r.get(k) for r in runs]) for k in int_keys})
        return merged

    # A qualifier, not a measurement: medianing a bool is meaningless. If any
    # run of this condition saw a larger untimed LCP candidate, the merged
    # median LCP is a lower bound and must say so.
    cwp_merged = _merge(cwp_runs, float_keys=cwp_keys)
    cwp_merged["lcp_underestimated"] = any(
        bool(r.get("lcp_underestimated")) for r in cwp_runs
    )

    return {
        "cwp": cwp_merged,
        # Lighthouse category scores are integers 0-100.
        "lighthouse": _merge(lh_runs, int_keys=lh_keys),
        "network": _merge(net_runs, float_keys=net_float_keys, int_keys=net_int_keys),
        "main_thread": _merge(mt_runs, float_keys=mt_float_keys, int_keys=mt_int_keys),
    }


def median_measurement(measurements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The raw measurement whose LCP is closest to the median (picks artifacts)."""
    if not measurements:
        return {}
    lcps = [
        m["cwp"]["lcp_ms"] for m in measurements
        if m.get("cwp", {}).get("lcp_ms") is not None
    ]
    if not lcps:
        return measurements[0]
    target = statistics.median(lcps)
    return min(
        measurements,
        key=lambda m: abs((m.get("cwp", {}).get("lcp_ms") or target) - target),
    )


def plan_conditions(
    cfg: ProjectConfig,
    *,
    device: Optional[str] = None,
    network: Optional[str] = None,
    runs: Optional[int] = None,
    pages: Optional[List[str]] = None,
) -> List[Tuple[PageTarget, PageTest]]:
    """Resolve the campaign matrix (apply overrides + page filter)."""
    selected_pages = list(cfg.pages)
    if pages:
        wanted = set(pages)
        missing = wanted - {p.name for p in cfg.pages}
        if missing:
            raise ValueError(f"Unknown page name(s): {', '.join(sorted(missing))}")
        selected_pages = [p for p in cfg.pages if p.name in wanted]

    if runs is not None and runs < 1:
        raise ValueError("--runs must be >= 1")

    plan: List[Tuple[PageTarget, PageTest]] = []
    for page in selected_pages:
        seen: set = set()
        for t in page.tests:
            cond = PageTest(
                device=device or t.device,
                network=network or t.network,
                runs=runs if runs is not None else t.runs,
            )
            key = (cond.device, cond.network)
            if key in seen:  # overrides may collapse conditions; dedupe
                continue
            seen.add(key)
            plan.append((page, cond))
    return plan

def _new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def make_automated_run(
    cfg: ProjectConfig,
    page: PageTarget,
    condition: PageTest,
    measurements: List[Dict[str, Any]],
    *,
    runner: str = DEFAULT_RUNNER_NAME,
) -> Run:
    """Emit the ONE canonical ``Run`` for a (page x condition) from N raw runs."""
    med = merge_median_metrics(measurements)
    representative = median_measurement(measurements)
    device = cfg.devices.get(condition.device)
    cpu_throttle = device.cpu_throttle if device else 1.0

    payload = {
        "run_id": _new_run_id(),
        "project": {"name": cfg.project, "url": page.url},
        "page": {"name": page.name, "url": page.url},
        "condition": {
            "device": condition.device,
            "network": condition.network,
            "cpu_throttle": cpu_throttle,
            "runs": condition.runs,
        },
        "meta": {"created_at": _now_iso(), "source": "automated", "runner": runner},
        "problem": {},
        "metrics": med,
        "resource_timings": representative.get("resource_timings", []),
        "captures": representative.get("captures", {}),
    }
    # Force through the canonical schema (automated CWV trio is enforced here).
    return Run.model_validate(payload)


def run_campaign(
    cfg: ProjectConfig,
    runner,
    *,
    device: Optional[str] = None,
    network: Optional[str] = None,
    runs: Optional[int] = None,
    pages: Optional[List[str]] = None,
    artifacts_root: Optional[str] = None,
    no_headers: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> List[Run]:
    """Run the full campaign; return one normalized Run per (page x condition).

    ``no_headers=True`` discards any configured request headers for this
    invocation — useful for measuring the same targets with and without a bot
    allowlist token. Headers are resolved per page only when they are actually
    wanted, so an unset token cannot break a campaign that does not use it.
    """
    plan = plan_conditions(cfg, device=device, network=network, runs=runs, pages=pages)
    result: List[Run] = []

    for page, condition in plan:
        device_obj = cfg.devices[condition.device]
        network_obj = cfg.networks[condition.network]
        headers = {} if no_headers else cfg.headers_for(page, env=env)
        measurements: List[Dict[str, Any]] = []
        for i in range(condition.runs):
            artifacts_dir = None
            if artifacts_root:
                artifacts_dir = str(
                    Path(artifacts_root)
                    / page.name
                    / f"{condition.device}__{condition.network}"
                    / f"run_{i + 1}"
                )
            token = f"run_{i + 1}"
            measurements.append(
                runner.run_condition(
                    page.url,
                    device_obj,
                    network_obj,
                    artifacts_dir=artifacts_dir,
                    run_id=token,
                    extra_http_headers=headers,
                )
            )
        result.append(make_automated_run(cfg, page, condition, measurements))
    return result


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest-automated",
        description="Multi-page browser campaign -> one normalized Run per (page x condition).",
    )
    p.add_argument("--device", help="Override device for every condition.")
    p.add_argument("--network", help="Override network for every condition.")
    p.add_argument("--runs", type=int, help="Override per-condition run count (>=1).")
    p.add_argument("--pages", help="Comma-separated page names to test.")
    p.add_argument("--artifacts-root", default="data/raw",
                   help="Root dir for HAR/trace/screenshot artifacts.")
    p.add_argument("--output-dir", default="data/processed",
                   help="Dir to write one JSON file per emitted run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve the matrix + write runs without a browser (for CI yet overridable).")
    p.add_argument("--no-headers", action="store_true",
                   help="Ignore any request headers configured in targets.yaml "
                        "for this run (e.g. to measure without a bot-allowlist token).")
    return p


def _real_runner(cfg: Optional[ProjectConfig] = None):
    """Build a real BrowserRunner (launches headless Chromium)."""
    from playwright.sync_api import sync_playwright  # local import for testability
    from ingest.browser.runner import BrowserRunner

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    kwargs: Dict[str, Any] = {}
    if cfg is not None:
        t = cfg.settings.timeouts
        kwargs = {
            "navigation_timeout_ms": t.navigation_ms,
            "network_idle_timeout_ms": t.network_idle_ms,
            "lcp_timeout_ms": t.lcp_ms,
            "inp_timeout_ms": t.inp_ms,
        }
    return pw, browser, BrowserRunner(browser, **kwargs)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point; returns a process exit code (0 = success)."""
    from config.load import load_config

    # Load .env (gitignored) so ${VAR} header references resolve without the
    # caller having to export secrets into the shell. Never overrides a value
    # already set in the real environment.
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:  # pragma: no cover - python-dotenv is a pinned dependency
        pass

    parser = _build_parser()
    args = parser.parse_args(argv)

    pages = None
    if args.pages:
        pages = [p.strip() for p in args.pages.split(",") if p.strip()]

    try:
        cfg = load_config()
    except Exception as exc:  # ConfigError
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        plan = plan_conditions(
            cfg,
            device=args.device,
            network=args.network,
            runs=args.runs,
            pages=pages,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        runs: List[Run] = []
        out_dir = Path(args.output_dir)
        for page, cond in plan:  # dry-run still emits nothing real; print plan
            print(f"{page.name}\t{cond.device}\t{cond.network}\t{cond.runs}")
        return 0

    pw = browser = None
    runner = None
    try:
        pw, browser, runner = _real_runner(cfg)
        runs = run_campaign(
            cfg,
            runner,
            device=args.device,
            network=args.network,
            runs=args.runs,
            pages=pages,
            artifacts_root=args.artifacts_root,
            no_headers=args.no_headers,
        )
    except Exception as exc:
        print(f"error: campaign failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if browser is not None:
                browser.close()
        finally:
            if pw is not None:
                pw.stop()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for run in runs:
        target = out_dir / f"{run.page.name}__{run.condition.device}__{run.condition.network}.json"
        target.write_text(json.dumps(run.model_dump(mode="json"), indent=2), encoding="utf-8")
        print(target)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())

