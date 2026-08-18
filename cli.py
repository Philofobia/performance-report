"""``python -m cli`` — one entry point for the whole pipeline.

A façade, deliberately. It consumes the command token and forwards the rest of
argv **verbatim** to the stage's existing ``main(argv) -> int``, returning that
exit code untouched. It computes nothing and validates nothing.

Why not argparse subparsers: registering the stages as subparsers means
redeclaring every flag they own — around thirty for ``ingest manual`` alone —
in a second place. That duplication is wrong the first time a stage grows an
option, and nothing fails when it does; the façade simply cannot pass the new
flag through. Verbatim forwarding has no such surface, and it buys real
help too: ``python -m cli report --help`` prints the report parser's own help,
so per-command help cannot drift from the parser it documents.

Every stage keeps its direct entry point. ``python -m analysis --no-llm`` is
still valid; this is an additional door, not a replacement one.
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, List, Optional

Delegate = Callable[[List[str]], int]
Loader = Callable[[], Delegate]

#: Command → one-line description. The only place this module says anything
#: about what the stages do; everything else is their parsers' business.
COMMANDS: Dict[str, str] = {
    "ingest auto": "Run a headless browser campaign over the configured matrix",
    "ingest manual": "Record a run from hand-supplied metrics",
    "analyze": "Turn stored runs into a Report JSON",
    "report": "Render a Report JSON to HTML, Markdown and PDF",
    "list-runs": "List the runs held in the SQLite run store",
    "ui": "Serve the local manual-entry form (loopback only)",
}

_INGEST_MODES = ("auto", "manual")


def _ingest_auto() -> Delegate:
    from ingest.automated import main
    return main


def _ingest_manual() -> Delegate:
    from ingest.manual import main
    return main


def _analyze() -> Delegate:
    from analysis.__main__ import main
    return main


def _report() -> Delegate:
    from report.__main__ import main
    return main


def _list_runs() -> Delegate:
    from store.listing import main
    return main


def _ui() -> Delegate:
    from webui.__main__ import main
    return main


#: Imports are deferred into these loaders so ``list-runs`` pays for neither
#: Playwright nor matplotlib.
_DELEGATES: Dict[str, Loader] = {
    "ingest auto": _ingest_auto,
    "ingest manual": _ingest_manual,
    "analyze": _analyze,
    "report": _report,
    "list-runs": _list_runs,
    "ui": _ui,
}


def usage() -> str:
    """The command table. Per-command help comes from the stage itself."""
    width = max(len(name) for name in COMMANDS)
    lines = [
        "usage: python -m cli <command> [options]",
        "",
        "commands:",
    ]
    lines += [f"  {name:<{width}}  {text}" for name, text in COMMANDS.items()]
    lines += [
        "",
        "Options are passed straight through to the command, so",
        "`python -m cli report --help` shows the report stage's own help.",
    ]
    return "\n".join(lines)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    print(usage(), file=sys.stderr)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0

    command, rest = argv[0], argv[1:]

    if command == "ingest":
        if not rest or rest[0].startswith("-"):
            return _fail(
                "ingest needs a mode: " + " or ".join(_INGEST_MODES)
            )
        command, rest = f"ingest {rest[0]}", rest[1:]
        if command not in _DELEGATES:
            return _fail(
                f"unknown ingest mode: {command.split(' ', 1)[1]} "
                f"(expected {' or '.join(_INGEST_MODES)})"
            )
    elif command not in _DELEGATES:
        return _fail(f"unknown command: {command}")

    return _DELEGATES[command]()(rest)


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
