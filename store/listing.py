"""``list-runs`` — what is in the store, before you analyse it.

The one genuinely new command in Phase 6. It lives in ``store/`` rather than in
the CLI façade for two reasons: the façade forwards argv verbatim to a stage's
``main(argv)`` and has no special cases, and the question this answers — what
runs do I have — is the store's question.

Formatting is separated from I/O so the table can be tested without a database.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from config.load import ConfigError, load_settings
from normalize.schema import Run
from store import sql

#: Printed for a metric the run does not carry. Never ``0``: a run with no INP
#: entry is a missing measurement, not a perfect one.
EMPTY = "—"

DEFAULT_LIMIT = 20

_COLUMNS = ("RUN ID", "PAGE", "DEVICE", "NETWORK", "LCP", "CLS", "INP")


def _cells(run: Run) -> List[str]:
    cwp = run.metrics.cwp
    return [
        run.run_id,
        run.page.name,
        run.condition.device,
        run.condition.network,
        EMPTY if cwp.lcp_ms is None else f"{cwp.lcp_ms:.0f}",
        EMPTY if cwp.cls is None else f"{cwp.cls:.2f}",
        EMPTY if cwp.inp_ms is None else f"{cwp.inp_ms:.0f}",
    ]


def format_run_table(runs: Sequence[Run]) -> str:
    """Render stored runs as an aligned table.

    Pure: no database, no settings, no clock. Widths come from the data so a
    long run id widens its column instead of shifting every column after it.
    """
    rows = [list(_COLUMNS)] + [_cells(run) for run in runs]
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in rows
    )


def _default_db() -> str:
    """The configured store path, or the schema default if config is unusable.

    A broken ``settings.yaml`` should not stop you listing runs — the stages
    that actually depend on the config already report it clearly.
    """
    try:
        return load_settings().storage.sqlite_path
    except (ConfigError, OSError):
        return "data/processed/runs.sqlite"


def query(
    conn,
    *,
    pages: Optional[Sequence[str]] = None,
    device: Optional[str] = None,
    network: Optional[str] = None,
    limit: Optional[int] = DEFAULT_LIMIT,
) -> List[Run]:
    """Stored runs, newest first, optionally narrowed.

    ``store.sql.list_runs`` filters one page at a time, so several names mean
    several queries; the limit is applied to the merged result rather than per
    page, which is what ``--limit 5`` reads as.
    """
    if not pages:
        return sql.list_runs(conn, device=device, network=network, limit=limit)

    merged: List[Run] = []
    seen = set()
    for page in pages:
        for run in sql.list_runs(conn, page=page, device=device, network=network):
            if run.run_id not in seen:
                seen.add(run.run_id)
                merged.append(run)
    merged.sort(key=lambda run: (run.meta.created_at, run.run_id), reverse=True)
    return merged[:limit] if limit else merged


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m cli list-runs",
        description="List the runs stored in the SQLite run store.",
    )
    p.add_argument("--db", default=None,
                   help="Run store path (default: settings storage.sqlite_path).")
    p.add_argument("--pages", default=None,
                   help="Comma-separated page names to list.")
    p.add_argument("--device", default=None, help="Only runs on this device.")
    p.add_argument("--network", default=None, help="Only runs on this network.")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"Maximum rows (default {DEFAULT_LIMIT}; 0 for all).")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    db = Path(args.db) if args.db else Path(_default_db())
    if not db.is_file():
        # sql.connect() would create it. Silently reporting an empty listing
        # for a mistyped path is worse than failing.
        print(f"No run store at {db}", file=sys.stderr)
        return 1

    pages = [p.strip() for p in args.pages.split(",") if p.strip()] if args.pages else None

    try:
        conn = sql.connect(db)
        try:
            runs = query(conn, pages=pages, device=args.device,
                         network=args.network, limit=args.limit or None)
        finally:
            conn.close()
    except (sql.StoreError, sqlite3.Error, OSError) as exc:
        print(f"Could not read {db}: {exc}", file=sys.stderr)
        return 1

    if not runs:
        print("no runs stored")
        return 0

    print(format_run_table(runs))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
