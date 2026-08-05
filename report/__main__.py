"""``python -m report`` — Report JSON in, deliverable out.

Reachable both directly and as ``python -m cli report``: the Phase 6 façade
forwards argv here verbatim rather than redeclaring these flags, so this
parser stays the single definition of the report stage's interface.

``--no-pdf`` exists so the whole pipeline can be exercised without Chromium,
the same courtesy ``--no-llm`` provides in the analysis layer.

``--skeleton-check`` lives here rather than in the façade for the same reason:
it belongs to the stage that renders. It is the user-facing half of
``report/skeleton.py`` — the half that turns "the skeleton never changes" from
a claim into a non-zero exit code.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional, Union

from pydantic import ValidationError

from analysis.reportmodel import Report
from report.render_html import render_html
from report.render_md import render_md
from report.skeleton import (
    BASELINE_PATH,
    diff_sections,
    fingerprint,
    format_drift,
    load_baseline,
    save_baseline,
)

DEFAULT_REPORTS_DIR = "data/reports"
REPORT_FILENAME = "report.json"


def find_report(
    *,
    input_path: Optional[Union[str, Path]] = None,
    campaign: Optional[str] = None,
    reports_dir: Union[str, Path] = DEFAULT_REPORTS_DIR,
) -> Path:
    """Locate the report.json to render.

    Explicit path wins; then a named campaign; then the most recently written
    report under ``reports_dir`` — which is almost always the one just
    produced by ``python -m analysis``.
    """
    if input_path is not None:
        path = Path(input_path)
        if not path.is_file():
            raise FileNotFoundError(f"Report not found: {path}")
        return path

    root = Path(reports_dir)
    if campaign is not None:
        path = root / campaign / REPORT_FILENAME
        if not path.is_file():
            raise FileNotFoundError(f"Report not found for campaign: {path}")
        return path

    candidates = sorted(
        root.glob(f"*/{REPORT_FILENAME}"),
        key=lambda p: (p.stat().st_mtime, str(p)),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No {REPORT_FILENAME} found under {root}")
    return candidates[0]


def load_report(path: Union[str, Path]) -> Report:
    """Read and validate a report.json against the Phase 4 schema.

    Validation happens here rather than in the template so a truncated or
    hand-edited document fails at the boundary with a useful message, instead
    of rendering a plausible-looking half-report.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    try:
        return Report.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"{path} is not a valid report: {exc}") from exc


def write_outputs(
    report: Report,
    *,
    output_dir: Path,
    with_pdf: bool,
    images: Optional[Mapping[str, Any]] = None,
) -> List[Path]:
    """Write report.html, report.md and optionally report.pdf."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    html = render_html(report, images=images)
    html_path = output_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    written.append(html_path)

    md_path = output_dir / "report.md"
    md_path.write_text(render_md(report, base_dir=output_dir), encoding="utf-8")
    written.append(md_path)

    if with_pdf:
        from report.render_pdf import chromium_page_factory, render_pdf

        pdf_path = output_dir / "report.pdf"
        pdf_path.write_bytes(render_pdf(html, page_factory=chromium_page_factory))
        written.append(pdf_path)

    return written


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m report",
        description="Render a Report JSON to HTML, Markdown and PDF.",
    )
    p.add_argument("--input", default=None,
                   help="Path to a report.json. Overrides --campaign.")
    p.add_argument("--campaign", default=None,
                   help="Campaign id under the reports directory.")
    p.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR,
                   help="Where campaigns live (default data/reports).")
    p.add_argument("--output-dir", default=None,
                   help="Where to write outputs (default: the input's directory).")
    p.add_argument("--no-pdf", action="store_true",
                   help="Skip PDF generation; no browser is launched.")
    p.add_argument("--no-appendix-images", action="store_true",
                   help="Do not embed screenshots. A capture of an "
                        "authenticated page shows whatever was on screen, and "
                        "the PDF gets emailed.")
    p.add_argument("--baseline", default=str(BASELINE_PATH),
                   help="Skeleton baseline file (default report/skeleton.baseline.json).")
    skeleton = p.add_mutually_exclusive_group()
    skeleton.add_argument("--skeleton-check", action="store_true",
                          help="Fail if the rendered structure drifted from the baseline.")
    skeleton.add_argument("--update-baseline", action="store_true",
                          help="Rewrite the baseline from this render. Commit the diff.")
    return p


def check_skeleton(html: str, *, baseline: Union[str, Path]) -> int:
    """Compare a rendered report against the committed baseline.

    Returns the process exit code. The caller has already written the report
    by this point, deliberately: the rendered output is the evidence for
    diagnosing drift, and the command that detected the problem should not be
    the one that withholds the artifact needed to understand it.
    """
    try:
        expected = load_baseline(baseline)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    changes = diff_sections(expected, fingerprint(html))
    if changes:
        print(format_drift(changes, path=baseline), file=sys.stderr)
        return 1
    print(f"skeleton ok: {len(expected)} sections match {baseline}")
    return 0


def update_baseline(html: str, *, baseline: Union[str, Path]) -> int:
    """Rewrite the baseline from this render."""
    sections = fingerprint(html)
    try:
        save_baseline(sections, baseline)
    except OSError as exc:
        print(f"Could not write {baseline}: {exc}", file=sys.stderr)
        return 1
    print(f"baseline updated: {len(sections)} sections written to {baseline}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        source = find_report(
            input_path=args.input,
            campaign=args.campaign,
            reports_dir=args.reports_dir,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        report = load_report(source)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    images = None
    if not args.no_appendix_images:
        from config.load import ConfigError, load_settings
        from report.images import build_appendix_images

        # Rendering an existing report.json must not depend on the config
        # being loadable — the report is already assembled, and the appendix
        # degrades to path-only rows exactly as it does for a missing capture.
        try:
            settings = load_settings()
        except (ConfigError, OSError, UnicodeDecodeError) as exc:
            # ConfigError covers missing files and malformed YAML/schema, but
            # `load_yaml` only wraps `yaml.YAMLError` — it does not wrap the
            # open/read itself, so a permission error, a broken symlink, or a
            # settings.yaml saved in a non-UTF-8 encoding (plausible on
            # Windows) would otherwise propagate uncaught and crash the
            # render of an already-valid report.json.
            print(f"Appendix images skipped: {exc}", file=sys.stderr)
        else:
            appendix_cfg = settings.report.appendix
            images = build_appendix_images(
                report,
                root=Path(settings.storage.raw_dir).resolve(),
                width=appendix_cfg.screenshot_width_px,
                max_height=appendix_cfg.screenshot_max_height_px,
            )

    output_dir = Path(args.output_dir) if args.output_dir else source.parent
    try:
        written = write_outputs(
            report, output_dir=output_dir, with_pdf=not args.no_pdf,
            images=images,
        )
    except OSError as exc:
        print(f"Could not write the report: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"PDF rendering failed: {exc}", file=sys.stderr)
        return 1

    for path in written:
        print(path)
    print(
        f"{len(report.pages)} page(s), verdict={report.cover.verdict}, "
        f"mode={report.meta.analysis_mode}"
    )

    if args.skeleton_check or args.update_baseline:
        # Fingerprint what was actually written, not a second render of the
        # same model — the guarantee is about the shipped artifact.
        html = (output_dir / "report.html").read_text(encoding="utf-8")
        if args.update_baseline:
            return update_baseline(html, baseline=args.baseline)
        return check_skeleton(html, baseline=args.baseline)

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
