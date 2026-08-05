"""Render the Report JSON to a Markdown mirror of the PDF.

"Mirror" means the same sections in the same order, carrying the same claims —
so a reviewer reading the Markdown in a pull request and a stakeholder reading
the PDF are looking at one document, not two that drifted apart.

Charts are omitted rather than approximated: the tables beneath each chart in
the HTML already carry the underlying numbers, and ASCII art of a donut would
be decoration pretending to be data.

**Escaping.** This environment deliberately does *not* autoescape — the output
is Markdown, and escaping ``<`` in ordinary prose would corrupt it. That makes
conversion of this file to HTML the consumer's escaping boundary, not ours.
The HTML rendering path in ``render_html.py`` does escape, and is the one this
system publishes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from jinja2 import Environment, FileSystemLoader

from analysis.reportmodel import Report
from report.render_html import metric_label, transfer_size

TEMPLATE_DIR = Path(__file__).parent / "template"
MD_TEMPLATE = "report.md.j2"

# The `##` heading sequence, mirroring the HTML skeleton minus the chart-only
# blocks. Asserted by the tests, so a template edit that drops a section fails
# rather than silently shipping a shorter document.
MD_SECTIONS: Tuple[str, ...] = (
    "Executive summary",
    "Pages",
    "Cross-page comparison",
    "Methodology",
    "Appendix",
)


def _link_path(path: Optional[str], base_dir: Optional[Path]) -> str:
    """A screenshot path as the Markdown should link it.

    Relative to the report when a relative path exists, absolute otherwise.
    On Windows a report on ``C:`` and artifacts on ``D:`` have no relative
    path at all, and ``os.path.relpath`` raises rather than returning
    something usable — so the absolute path is the honest fallback, not a
    failure.
    """
    if not path:
        return ""
    target = Path(path)
    if base_dir is not None:
        try:
            return Path(os.path.relpath(target, base_dir)).as_posix()
        except ValueError:
            pass
    return target.as_posix()


def _table_cell(value: Optional[object]) -> object:
    """Escape ``|`` so an arbitrary string can sit inside a GFM table cell.

    A pipe is legal in a URL's query string or fragment; interpolated
    unescaped, it opens a new column and shifts everything after it.
    ``render_md`` runs with autoescape off by design (see the module
    docstring) — that boundary protects prose from HTML-escaping, not table
    cells from GFM's own delimiter, so this is a separate, narrower escape
    applied only to cell text.
    """
    if value is None:
        return value
    return str(value).replace("|", "\\|")


def _env(base_dir: Optional[Path] = None) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,  # Markdown, not HTML — see module docstring
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # The same filters the HTML path uses, so the mirror names metrics and
    # byte counts identically rather than formatting them a second way.
    env.filters["metric_label"] = metric_label
    env.filters["transfer_size"] = transfer_size
    env.filters["link_path"] = lambda p: _link_path(p, base_dir)
    env.filters["table_cell"] = _table_cell
    return env


def render_md(report: Report, *, base_dir: Optional[Path] = None) -> str:
    """Render the Markdown mirror.

    ``base_dir`` is where the ``report.md`` will be written, used to link
    screenshots relatively. The mirror links rather than embeds: a data URI
    that makes sense in a self-contained HTML file is megabytes of noise in a
    document meant to be read as text in a pull request.
    """
    return _env(base_dir).get_template(MD_TEMPLATE).render(report=report)
