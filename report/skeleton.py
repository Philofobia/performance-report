"""Structural fingerprint of a rendered report (PROJECT_SPEC §6.2).

The project's headline promise is that the report skeleton never changes —
same sections, same order, every run. Proving that needs a comparison which
survives the data changing, and that rules out diffing rendered bytes: two
runs of the *same* campaign matching only proves the renderer is a pure
function. It would pass happily while a section silently vanished for every
campaign, which is exactly how a skeleton rots.

So the template tags every structural block with ``data-section``, this module
reads those tags in document order, and the repeating per-page block collapses
to a single ``page[]`` group. A one-page report and a five-page report then
have *identical* fingerprints, and the test comparing them catches a section
that disappears whenever its list happens to be empty.

Phase 6 wires this to a ``--skeleton-check`` flag comparing against a
committed baseline; the function lives here because the template it guards
lives here.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Sequence

PAGE_GROUP = "page[]"

_PAGE_ROOT = "page"
_PAGE_CHILD_PREFIX = "page."


class _SectionCollector(HTMLParser):
    """Collects ``data-section`` values in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: List[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        for name, value in attrs:
            if name == "data-section" and value:
                self.sections.append(value)


def collapse(sections: Sequence[str]) -> List[str]:
    """Fold repeated per-page blocks into one ``page[]`` group.

    Only the *first* page block contributes its children, so the result is
    independent of how many pages the campaign covered — which is the property
    that makes cross-campaign comparison meaningful.
    """
    out: List[str] = []
    emitted_page_block = False
    index = 0
    while index < len(sections):
        section = sections[index]
        if section != _PAGE_ROOT:
            out.append(section)
            index += 1
            continue

        block = [PAGE_GROUP]
        index += 1
        while index < len(sections) and sections[index].startswith(_PAGE_CHILD_PREFIX):
            block.append(sections[index])
            index += 1
        if not emitted_page_block:
            out.extend(block)
            emitted_page_block = True
    return out


def fingerprint(html: str) -> List[str]:
    """The ordered section list for a rendered report."""
    collector = _SectionCollector()
    collector.feed(html)
    collector.close()
    return collapse(collector.sections)
