"""``python -m report`` — Report JSON in, deliverable out.

An interim entry point, exactly as ``python -m analysis`` is: Phase 6 folds
both into a unified ``src/cli.py``. Until then this is how rendering is driven
by hand and by the integration tests.

``--no-pdf`` exists so the whole pipeline can be exercised without Chromium,
the same courtesy ``--no-llm`` provides in the analysis layer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Union

from pydantic import ValidationError

from analysis.reportmodel import Report
from report.render_html import render_html
from report.render_md import render_md

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


def write_outputs(report: Report, *, output_dir: Path, with_pdf: bool) -> List[Path]:
    """Write report.html, report.md and optionally report.pdf."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    html = render_html(report)
    html_path = output_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    written.append(html_path)

    md_path = output_dir / "report.md"
    md_path.write_text(render_md(report), encoding="utf-8")
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
    return p


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

    output_dir = Path(args.output_dir) if args.output_dir else source.parent
    try:
        written = write_outputs(
            report, output_dir=output_dir, with_pdf=not args.no_pdf
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
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
