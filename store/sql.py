"""SQLite persistence for canonical runs (PROJECT_SPEC §9, Phase 3).

One row per canonical :class:`normalize.schema.Run` in ``runs``, with resource
timings in a child table. Metrics are stored as flat, queryable columns — not a
JSON blob — so the report layer can ask cross-run questions ("LCP per page per
condition over time") in SQL rather than loading every run into memory.

The full run JSON is *also* kept in ``payload`` so a stored run round-trips back
to an identical ``Run`` even as the schema gains fields; the flat columns are a
query index over that source of truth.

Connections are created with ``detect_types`` off and explicit parameter
binding everywhere — no string-interpolated SQL.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from normalize.schema import Run

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    project_name      TEXT NOT NULL,
    project_url       TEXT NOT NULL,
    page_name         TEXT NOT NULL,
    page_url          TEXT NOT NULL,
    device            TEXT NOT NULL,
    network           TEXT NOT NULL,
    cpu_throttle      REAL,
    run_count         INTEGER,
    created_at        TEXT NOT NULL,
    source            TEXT NOT NULL,
    runner            TEXT,
    problem           TEXT,

    lcp_ms            REAL,
    cls               REAL,
    inp_ms            REAL,
    fcp_ms            REAL,
    ttfb_ms           REAL,
    tbt_ms            REAL,

    lh_performance    INTEGER,
    lh_accessibility  INTEGER,
    lh_best_practices INTEGER,
    lh_seo            INTEGER,

    total_transfer_kb REAL,
    request_count     INTEGER,
    render_blocking_css INTEGER,

    script_ms         REAL,
    layout_ms         REAL,
    style_ms          REAL,
    task_ms           REAL,
    js_heap_kb        REAL,
    dom_nodes         INTEGER,

    payload           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_project_page
    ON runs (project_name, page_name);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs (created_at);

CREATE TABLE IF NOT EXISTS resource_timings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    type        TEXT,
    transfer_kb REAL,
    duration_ms REAL
);

CREATE INDEX IF NOT EXISTS idx_timings_run ON resource_timings (run_id);
"""


class StoreError(Exception):
    """User-facing error for storage failures."""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open (and initialize) a runs database.

    ``:memory:`` is supported for tests. Parent directories are created for
    file-backed databases.
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if absent and stamp the schema version."""
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()


def _row_values(run: Run) -> Dict[str, Any]:
    """Flatten a canonical Run into the ``runs`` column set."""
    cwp = run.metrics.cwp
    lh = run.metrics.lighthouse
    net = run.metrics.network
    mt = run.metrics.main_thread
    return {
        "run_id": run.run_id,
        "project_name": run.project.name,
        "project_url": run.project.url,
        "page_name": run.page.name,
        "page_url": run.page.url,
        "device": run.condition.device,
        "network": run.condition.network,
        "cpu_throttle": run.condition.cpu_throttle,
        "run_count": run.condition.runs,
        "created_at": run.meta.created_at.isoformat(),
        "source": run.meta.source,
        "runner": run.meta.runner,
        "problem": run.problem.description,
        "lcp_ms": cwp.lcp_ms,
        "cls": cwp.cls,
        "inp_ms": cwp.inp_ms,
        "fcp_ms": cwp.fcp_ms,
        "ttfb_ms": cwp.ttfb_ms,
        "tbt_ms": cwp.tbt_ms,
        "lh_performance": lh.performance,
        "lh_accessibility": lh.accessibility,
        "lh_best_practices": lh.best_practices,
        "lh_seo": lh.seo,
        "total_transfer_kb": net.total_transfer_kb,
        "request_count": net.request_count,
        "render_blocking_css": net.render_blocking_css,
        "script_ms": mt.script_ms,
        "layout_ms": mt.layout_ms,
        "style_ms": mt.style_ms,
        "task_ms": mt.task_ms,
        "js_heap_kb": mt.js_heap_kb,
        "dom_nodes": mt.dom_nodes,
        "payload": json.dumps(run.model_dump(mode="json"), sort_keys=True),
    }


def insert_run(conn: sqlite3.Connection, run: Run, *, replace: bool = False) -> str:
    """Persist one canonical run (plus its resource timings). Returns the run_id.

    Re-inserting an existing ``run_id`` raises unless ``replace=True`` — a
    silent overwrite would destroy an auditable measurement.
    """
    values = _row_values(run)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    try:
        with conn:
            if replace:
                conn.execute(
                    "DELETE FROM resource_timings WHERE run_id = ?", (run.run_id,)
                )
            conn.execute(
                f"{verb} INTO runs ({columns}) VALUES ({placeholders})", values
            )
            conn.executemany(
                "INSERT INTO resource_timings (run_id, name, type, transfer_kb, duration_ms)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (run.run_id, t.name, t.type, t.transfer_kb, t.duration_ms)
                    for t in run.resource_timings
                ],
            )
    except sqlite3.IntegrityError as exc:
        raise StoreError(
            f"Run {run.run_id!r} already stored; pass replace=True to overwrite"
        ) from exc
    return run.run_id


def insert_runs(conn: sqlite3.Connection, runs: Iterable[Run], **kwargs) -> List[str]:
    """Persist a whole campaign's worth of runs."""
    return [insert_run(conn, run, **kwargs) for run in runs]


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[Run]:
    """Load one run back as a canonical ``Run``, or None if absent."""
    row = conn.execute(
        "SELECT payload FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    return Run.model_validate(json.loads(row["payload"]))


def list_runs(
    conn: sqlite3.Connection,
    *,
    project: Optional[str] = None,
    page: Optional[str] = None,
    device: Optional[str] = None,
    network: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Run]:
    """Query stored runs, newest first, with optional filters."""
    clauses: List[str] = []
    params: List[Any] = []
    for column, value in (
        ("project_name", project),
        ("page_name", page),
        ("device", device),
        ("network", network),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)

    sql = "SELECT payload FROM runs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, run_id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()
    return [Run.model_validate(json.loads(r["payload"])) for r in rows]


def get_resource_timings(conn: sqlite3.Connection, run_id: str) -> List[Dict[str, Any]]:
    """Resource timings for a run, largest transfer first (report §3 input)."""
    rows = conn.execute(
        "SELECT name, type, transfer_kb, duration_ms FROM resource_timings"
        " WHERE run_id = ? ORDER BY transfer_kb DESC, name ASC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def metric_history(
    conn: sqlite3.Connection,
    metric: str,
    *,
    project: Optional[str] = None,
    page: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Time series of one metric per page/condition — the trend-over-time input.

    ``metric`` is validated against the known column set rather than
    interpolated blindly (no SQL injection via a caller-supplied name).
    """
    allowed = {
        "lcp_ms", "cls", "inp_ms", "fcp_ms", "ttfb_ms", "tbt_ms",
        "lh_performance", "total_transfer_kb", "request_count",
        "script_ms", "task_ms", "dom_nodes",
    }
    if metric not in allowed:
        raise StoreError(
            f"Unknown metric {metric!r}; expected one of {', '.join(sorted(allowed))}"
        )

    clauses: List[str] = []
    params: List[Any] = []
    if project is not None:
        clauses.append("project_name = ?")
        params.append(project)
    if page is not None:
        clauses.append("page_name = ?")
        params.append(page)

    sql = (
        f"SELECT run_id, page_name, device, network, created_at, {metric} AS value"
        " FROM runs"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def delete_run(conn: sqlite3.Connection, run_id: str) -> bool:
    """Delete a run and its timings. Returns True if a row was removed."""
    with conn:
        cur = conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM resource_timings WHERE run_id = ?", (run_id,))
    return cur.rowcount > 0


def count_runs(conn: sqlite3.Connection) -> int:
    """Total stored runs."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])
