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

Phase 6 adds the other half: ``skeleton.baseline.json`` records the canonical
section list, ``report --skeleton-check`` diffs a rendered report against it,
and ``report --update-baseline`` rewrites it. Regenerating the baseline is a
deliberate act that lands as a reviewable diff, which is the whole mechanism by
which drift becomes visible rather than merely detectable. All of it lives here
because the template it guards lives here.
"""
from __future__ import annotations

import difflib
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Sequence, Tuple, Union

PAGE_GROUP = "page[]"

_PAGE_ROOT = "page"
_PAGE_CHILD_PREFIX = "page."

BASELINE_PATH = Path(__file__).with_name("skeleton.baseline.json")

#: Bumped when the *fingerprint algorithm* changes, never when the template
#: does. A baseline written by a different algorithm is rejected outright, so
#: an algorithm change cannot masquerade as every section having drifted.
BASELINE_VERSION = 1

#: ``("-", section, index)`` for a section the baseline has and the render does
#: not; ``("+", ...)`` for the reverse. Index is into the list it came from.
Change = Tuple[str, str, int]


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


def load_baseline(path: Union[str, Path] = BASELINE_PATH) -> List[str]:
    """Read the committed section list.

    Every failure mode names the path, because the most likely one is a
    mistyped ``--baseline`` rather than a corrupted file.
    """
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read the skeleton baseline {path}: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("sections"), list):
        raise ValueError(f"{path} is not a skeleton baseline document")

    version = payload.get("version")
    if version != BASELINE_VERSION:
        raise ValueError(
            f"{path} was written by fingerprint version {version!r}, "
            f"this build expects {BASELINE_VERSION}. Regenerate it with "
            "--update-baseline after reviewing the algorithm change."
        )
    return [str(section) for section in payload["sections"]]


def save_baseline(
    sections: Sequence[str], path: Union[str, Path] = BASELINE_PATH
) -> None:
    """Write the section list, one entry per line.

    The formatting is deliberate: a section added to the template should show
    up in review as a one-line diff, not as a reflowed blob.
    """
    document = {"version": BASELINE_VERSION, "sections": list(sections)}
    Path(path).write_text(
        json.dumps(document, indent=4) + "\n", encoding="utf-8"
    )


def diff_sections(expected: Sequence[str], actual: Sequence[str]) -> List[Change]:
    """Changes taking the baseline to the rendered fingerprint.

    ``SequenceMatcher`` rather than a positional walk, so a section that moved
    reads as one removal plus one addition instead of turning every section
    after it into a mismatch.
    """
    changes: List[Change] = []
    matcher = difflib.SequenceMatcher(a=list(expected), b=list(actual), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for offset, section in enumerate(expected[i1:i2]):
            changes.append(("-", section, i1 + offset))
        for offset, section in enumerate(actual[j1:j2]):
            changes.append(("+", section, j1 + offset))
    return changes


def format_drift(changes: Sequence[Change], *, path: Union[str, Path]) -> str:
    """The message ``--skeleton-check`` prints when the skeleton moved."""
    width = max((len(section) for _, section, _ in changes), default=0)
    lines = [f"skeleton drift vs {path}:"]
    for sign, section, index in changes:
        where = "expected at" if sign == "-" else "found at"
        lines.append(f"  {sign} {section:<{width}}  ({where} index {index})")
    return "\n".join(lines)
