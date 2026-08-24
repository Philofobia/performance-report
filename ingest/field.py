"""``python -m cli ingest field`` — Grafana in, FieldSnapshot in the store.

A third ingestion door beside ``ingest auto`` and ``ingest manual``. Unlike
those two it produces no :class:`Run`: field data is session-scoped and
brand-wide, so it lands in ``field_snapshots`` instead.

This stage **does** exit non-zero on failure, unlike analysis. A bad token, an
unreachable host or a rejected query are all things a user can fix, and the
existing convention is that those are errors.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence
from urllib.parse import urlsplit

from config.load import load_settings, load_targets
from ingest.grafana import parse, queries
from ingest.grafana.client import GrafanaClient, GrafanaError, resolve_grafana_env
from normalize.field import FieldSnapshot, window_delta


def hosts_for(targets: Any) -> List[str]:
    """The hostnames of every configured page, deduplicated and sorted.

    Derived rather than configured separately: a second list of hosts drifts,
    and when it does the report compares lab measurements of one site against
    field measurements of another while reading perfectly plausibly.
    """
    found = set()
    for page in getattr(targets, "pages", []):
        host = urlsplit(getattr(page, "url", "")).hostname
        if host:
            found.add(host.lower())
    return sorted(found)


def _build_client(settings: Any) -> GrafanaClient:
    """The real client. Separated so tests substitute it without patching env."""
    return GrafanaClient(
        resolve_grafana_env(), timeout_s=settings.grafana.timeout_s
    )


def fetch_snapshot(
    settings: Any,
    *,
    project: str,
    hosts: Sequence[str],
    table: Optional[str] = None,
    client: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> FieldSnapshot:
    """Render every query, issue one request, parse the result.

    ``table`` is resolved from the explicit argument first and the client's
    public ``.table`` property second — never through a private attribute,
    and never silently: a table that cannot be resolved raises rather than
    rendering a placeholder into SQL that would only fail once it reaches
    real ClickHouse.
    """
    client = client or _build_client(settings)
    table = table or getattr(client, "table", None)
    if not table:
        raise ValueError(
            "No ClickHouse table to query. Set GRAFANA_TABLE in .env."
        )
    window = settings.grafana.window
    fetched_at = now or datetime.now(timezone.utc)

    sql_by_ref = queries.render_all(table=table, hosts=hosts)
    results = client.query(sql_by_ref, window=window)

    return parse.build_snapshot(
        results,
        project=project,
        hosts=hosts,
        window_from=fetched_at - window_delta(window),
        window_to=fetched_at,
        fetched_at=fetched_at,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m cli ingest field",
        description="Fetch real-user (RUM) data from Grafana and store it.",
    )
    p.add_argument("--project", default=None,
                   help="Project name (default: from config/targets.yaml).")
    p.add_argument("--hosts", default=None,
                   help="Comma-separated hosts (default: derived from targets).")
    p.add_argument("--settings", default=None, help="Path to settings.yaml.")
    p.add_argument("--targets", default=None, help="Path to targets.yaml.")
    p.add_argument("--replace", action="store_true",
                   help="Overwrite a snapshot with the same id.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = load_settings(args.settings) if args.settings else load_settings()

    project = args.project
    hosts: List[str]
    if args.hosts:
        hosts = sorted({h.strip().lower() for h in args.hosts.split(",") if h.strip()})
    else:
        targets = load_targets(args.targets) if args.targets else load_targets()
        hosts = hosts_for(targets)
        project = project or targets.project

    if not project:
        print("No project name. Pass --project or configure targets.yaml.",
              file=sys.stderr)
        return 2
    if not hosts:
        print("No hosts to query. Pass --hosts or configure pages in "
              "targets.yaml.", file=sys.stderr)
        return 2

    try:
        snapshot = fetch_snapshot(settings, project=project, hosts=hosts)
    except (GrafanaError, ValueError, parse.ParseError, queries.HostError) as exc:
        print(f"Field ingestion failed: {exc}", file=sys.stderr)
        return 1

    from store import sql

    conn = sql.connect(settings.storage.sqlite_path)
    sql.init_schema(conn)
    try:
        sql.insert_snapshot(conn, snapshot, replace=args.replace)
    finally:
        conn.close()

    print(
        f"Stored field snapshot {snapshot.snapshot_id} for "
        f"{', '.join(snapshot.hosts)} covering "
        f"{snapshot.window_from:%Y-%m-%d} to {snapshot.window_to:%Y-%m-%d} "
        f"({len(snapshot.by_pagetype)} page groups, "
        f"{len(snapshot.by_country)} countries)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
