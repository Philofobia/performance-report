"""What makes a measured run durable: scrubbed artifacts, JSON, run store.

Everything this module coordinates already existed — ``store.artifacts``
scrubbed HARs, ``store.sql`` held runs, and ``ingest.automated`` wrote JSON —
but nothing in the pipeline connected them. The consequences were quiet rather
than loud: ``list-runs`` reported an empty store forever, every trend series
rendered as ``new`` because no history ever accumulated, and the raw HARs under
``data/raw`` kept their ``Cookie``, ``Authorization`` and bot-allowlist header
values because the scrubber was only ever reached by its own tests.

This is a sink, not a stage. ``run_campaign`` calls it once per completed
(page x condition) and takes back whatever it returns, which is how the Run
that reaches ``report.json`` ends up pointing at the *scrubbed* capture rather
than the temporary one the browser wrote.

Order matters here. Artifacts are stored first, so a run is never recorded
pointing at a capture that was not moved; the JSON is written next, because it
is what ``python -m analysis`` reads by default; the store insert is last, and
is the only step a campaign can survive losing.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional

from normalize.schema import Captures, Run
from store.artifacts import safe_segment, store_artifacts
from store.sql import insert_run


def run_filename(run: Run) -> str:
    """The run's JSON filename: one file per (page x condition).

    Every segment is passed through :func:`store.artifacts.safe_segment`. A
    page legitimately named ``checkout/step-2`` in ``targets.yaml`` would
    otherwise write outside the output directory — or, on Windows, not write
    at all.
    """
    return (
        f"{safe_segment(run.page.name)}"
        f"__{safe_segment(run.condition.device)}"
        f"__{safe_segment(run.condition.network)}.json"
    )


class RunPersister:
    """Persist one completed run. Call it; keep the Run it returns.

    ``store_root`` is where scrubbed captures land — omit it and captures are
    left exactly where the runner wrote them, which is what the artifact-less
    campaigns in the test suite want. ``conn`` is likewise optional: a campaign
    can produce run JSON without a run store, and a store failure must never be
    the reason a measurement is lost.
    """

    def __init__(
        self,
        *,
        output_dir: str | Path,
        store_root: Optional[str | Path] = None,
        conn: Optional[sqlite3.Connection] = None,
        extra_headers: Iterable[str] = (),
    ) -> None:
        self.output_dir = Path(output_dir)
        self.store_root = None if store_root is None else Path(store_root)
        self.conn = conn
        self.extra_headers = list(extra_headers)
        #: Paths written so far, in order — the CLI prints these as it goes so
        #: a campaign that dies halfway still tells you what it kept.
        self.written: List[Path] = []

    def __call__(self, run: Run) -> Run:
        run = self._store_captures(run)
        self._write_json(run)
        self._insert(run)
        return run

    def _store_captures(self, run: Run) -> Run:
        """Move captures into the store, scrubbing the HAR, and repoint the Run.

        ``move=True``: a copy leaves the unredacted original on disk, which is
        the whole thing the scrub exists to prevent.
        """
        if self.store_root is None:
            return run
        stored = store_artifacts(
            self.store_root, run, move=True, extra_headers=self.extra_headers
        )
        return run.model_copy(update={"captures": Captures(**stored)})

    def _write_json(self, run: Run) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.output_dir / run_filename(run)
        target.write_text(
            json.dumps(run.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        self.written.append(target)
        return target

    def _insert(self, run: Run) -> None:
        """Record the run in the store.

        ``replace=True`` because the JSON for this (page x condition) has just
        been overwritten too: re-running the same campaign twice updates both
        or neither. Run ids carry a uuid4 suffix, so this replaces a row only
        when the same run really is being written again.
        """
        if self.conn is None:
            return
        insert_run(self.conn, run, replace=True)
