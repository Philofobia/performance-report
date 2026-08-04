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

from pathlib import Path
from typing import Tuple

from jinja2 import Environment, FileSystemLoader

from analysis.reportmodel import Report
from report.render_html import metric_label

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
)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,  # Markdown, not HTML — see module docstring
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # The same filter the HTML path uses, so the mirror names metrics
    # identically rather than falling back to raw field names.
    env.filters["metric_label"] = metric_label
    return env


def render_md(report: Report) -> str:
    """Render the Markdown mirror."""
    return _env().get_template(MD_TEMPLATE).render(report=report)
