"""Free-tier spend control: a per-UTC-day ledger and the policy over it.

The pipeline's only defence against the free tier used to be reactive — retry
the 429, then degrade to rules. That protects the *last* run of a day, not the
first: by the time a quota error arrives the allowance is already gone, and
whichever campaign happened to be unlucky loses its prose. This module makes
the cost of a call knowable *before* it is made, so a run can be stopped from
spending quota that the day's first report needs.

Two objects, kept apart on purpose. :class:`DailyLedger` is storage and knows
nothing about limits; :class:`TokenBudget` is policy and is tested against
:class:`InMemoryLedger` without SQLite anywhere near it.

Counting is local (``len(text) / 4``) rather than Google's ``countTokens``
endpoint, because that endpoint is itself a request against the same free-tier
request limit — metering with it would spend the thing being rationed. The
estimate only decides whether to *start* a call; the ledger is corrected from
the response's ``usage_metadata`` afterwards, so estimation error never
compounds across a day.

What the free tier actually limits is worth stating, because it shaped the
schema: Google no longer publishes the numbers, and
https://ai.google.dev/gemini-api/docs/rate-limits says only that limits are
counted as requests/minute, input tokens/minute and requests/day. There is no
published tokens-per-day cap, so all three dimensions are tracked and all three
are configurable.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from config.load import BudgetConfig, ServiceBudget
from rag.embeddings import EmbeddingError

SERVICE_LLM = "llm"
SERVICE_EMBEDDINGS = "embeddings"

#: Rough characters-per-token ratio, close enough for prose and JSON alike.
CHARS_PER_TOKEN = 4

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_ledger (
    day_utc       TEXT NOT NULL,
    service       TEXT NOT NULL,
    model         TEXT NOT NULL,
    requests      INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day_utc, service, model)
);
"""


class BudgetExhaustedError(EmbeddingError):
    """The configured daily allowance for a service is spent.

    Subclasses :class:`EmbeddingError` so that any degradation path this
    project already has treats it as "no model available" rather than letting
    it escape and fail a run — the feature must never cost a report.
    """


@dataclass(frozen=True)
class Spend:
    """Requests and tokens: used both for what was spent and for what is left."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Usage:
    """Actual token counts reported by the API for one call."""

    input_tokens: int = 0
    output_tokens: int = 0


def estimate_tokens(text: str) -> int:
    """Rough token count, used only to decide whether a call is affordable."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


class InMemoryLedger:
    """Process-local ledger: the default when there is no store to write to."""

    def __init__(self) -> None:
        self._rows: Dict[Tuple[str, str, str], Spend] = {}

    def spent(self, day: str, service: str) -> Spend:
        total = Spend()
        for (row_day, row_service, _model), spend in self._rows.items():
            if row_day == day and row_service == service:
                total = Spend(
                    total.requests + spend.requests,
                    total.input_tokens + spend.input_tokens,
                    total.output_tokens + spend.output_tokens,
                )
        return total

    def add(self, day: str, service: str, model: str, *, requests: int,
            input_tokens: int, output_tokens: int) -> None:
        current = self._rows.get((day, service, model), Spend())
        self._rows[(day, service, model)] = Spend(
            current.requests + requests,
            current.input_tokens + input_tokens,
            current.output_tokens + output_tokens,
        )


class DailyLedger:
    """SQLite-backed ledger, living in the run store beside the runs it paid for.

    Rows are per ``(day, service, model)`` and :meth:`spent` sums across models,
    so switching model mid-day does not hand the next run a fresh allowance.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        conn.executescript(LEDGER_SCHEMA)
        conn.commit()

    def spent(self, day: str, service: str) -> Spend:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(requests), 0) AS r,"
            "       COALESCE(SUM(input_tokens), 0) AS i,"
            "       COALESCE(SUM(output_tokens), 0) AS o"
            " FROM token_ledger WHERE day_utc = ? AND service = ?",
            (day, service),
        ).fetchone()
        return Spend(int(row[0]), int(row[1]), int(row[2]))

    def add(self, day: str, service: str, model: str, *, requests: int,
            input_tokens: int, output_tokens: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO token_ledger"
                " (day_utc, service, model, requests, input_tokens, output_tokens)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(day_utc, service, model) DO UPDATE SET"
                "   requests = requests + excluded.requests,"
                "   input_tokens = input_tokens + excluded.input_tokens,"
                "   output_tokens = output_tokens + excluded.output_tokens",
                (day, service, model, requests, input_tokens, output_tokens),
            )


def _thousands(value: int) -> str:
    """Compact token counts for the status line: 38200 -> '38.2k'."""
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


class TokenBudget:
    """Decides whether a call may be made, and records what it cost.

    ``clock`` is injected so tests can cross UTC midnight deterministically —
    the same reason ``jitter`` is injected into the backoff schedule.
    """

    def __init__(
        self,
        config: BudgetConfig,
        *,
        ledger: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._config = config
        self._ledger = ledger if ledger is not None else InMemoryLedger()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- state ------------------------------------------------------------- #
    def today(self) -> str:
        """The current UTC day: the key every ledger row is filed under."""
        return self._clock().astimezone(timezone.utc).date().isoformat()

    def limits_for(self, service: str) -> ServiceBudget:
        return self._config.llm if service == SERVICE_LLM else self._config.embeddings

    def remaining(self, service: str) -> Spend:
        limits = self.limits_for(service)
        spend = self._ledger.spent(self.today(), service)
        return Spend(
            max(0, limits.daily_requests - spend.requests),
            max(0, limits.daily_input_tokens - spend.input_tokens),
            max(0, limits.daily_output_tokens - spend.output_tokens),
        )

    # -- policy ------------------------------------------------------------ #
    def reserve(self, service: str, *, estimated_input: int,
                estimated_output: int) -> None:
        """Refuse a call whose worst case would break today's allowance.

        Nothing is written here. A refused call costs nothing, and a
        reservation that is never spent must not leak into tomorrow — which is
        why the ledger is only ever written from :meth:`record`, after a call
        has actually returned.
        """
        if not self._config.enabled:
            return
        limits = self.limits_for(service)
        spend = self._ledger.spent(self.today(), service)
        for label, used, wanted, cap, key in (
            ("requests", spend.requests, 1, limits.daily_requests, "daily_requests"),
            ("input tokens", spend.input_tokens, estimated_input,
             limits.daily_input_tokens, "daily_input_tokens"),
            ("output tokens", spend.output_tokens, estimated_output,
             limits.daily_output_tokens, "daily_output_tokens"),
        ):
            if used + wanted > cap:
                raise BudgetExhaustedError(
                    f"Token budget for {service} is exhausted for {self.today()} "
                    f"(UTC): {label} {used}/{cap}, and this call needs {wanted} "
                    f"more. Raise budget.{service}.{key} in config/settings.yaml, "
                    "re-run with --no-budget to spend freely, or wait for the "
                    "next UTC day."
                )

    def record(self, service: str, model: str, *, input_tokens: int,
               output_tokens: int) -> None:
        """Write one request and its actual token cost to the ledger."""
        if not self._config.enabled:
            return
        self._ledger.add(
            self.today(), service, model or "unknown", requests=1,
            input_tokens=max(0, int(input_tokens)),
            output_tokens=max(0, int(output_tokens)),
        )

    # -- reporting --------------------------------------------------------- #
    def summary_line(self) -> str:
        """One line for the end of a run, and for ``--budget-status``."""
        parts = []
        for service in (SERVICE_LLM, SERVICE_EMBEDDINGS):
            limits = self.limits_for(service)
            spend = self._ledger.spent(self.today(), service)
            chunk = (
                f"{service} {_thousands(spend.input_tokens)}/"
                f"{_thousands(limits.daily_input_tokens)} in"
            )
            if limits.daily_output_tokens:
                chunk += (
                    f", {_thousands(spend.output_tokens)}/"
                    f"{_thousands(limits.daily_output_tokens)} out"
                )
            chunk += f", {spend.requests}/{limits.daily_requests} req"
            parts.append(chunk)
        return f"budget: {' | '.join(parts)}  ({self.today()}, UTC)"


def build_budget(settings: Any, *,
                 conn: Optional[sqlite3.Connection] = None) -> Optional[TokenBudget]:
    """Build the run's budget, or ``None`` when budgeting is switched off.

    A ledger that cannot be created is not fatal: bookkeeping must never cost a
    report, so an unusable database degrades to counting in memory for this run
    rather than raising.
    """
    if not settings.budget.enabled:
        return None
    ledger: Optional[Any] = None
    if conn is not None:
        try:
            ledger = DailyLedger(conn)
        except sqlite3.Error:
            ledger = None
    return TokenBudget(settings.budget, ledger=ledger)
