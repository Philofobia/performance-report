"""Unit tests for rag/budget.py.

No SDK, no key, no network (TESTING_PLAN.md §3). The clock is injected so the
UTC day rollover — the thing the whole ledger is keyed on — is asserted
deterministically rather than waited for.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from config.load import BudgetConfig, ServiceBudget
from rag.budget import (
    SERVICE_EMBEDDINGS,
    SERVICE_LLM,
    BudgetExhaustedError,
    DailyLedger,
    InMemoryLedger,
    TokenBudget,
    estimate_tokens,
)


def _config(**llm) -> BudgetConfig:
    base = dict(
        daily_requests=10,
        daily_input_tokens=1000,
        daily_output_tokens=500,
        max_output_tokens_per_call=100,
    )
    base.update(llm)
    return BudgetConfig(llm=ServiceBudget(**base))


class FakeClock:
    """A clock the tests can walk across midnight."""

    def __init__(self, stamp: str = "2026-08-20T09:00:00+00:00") -> None:
        self.stamp = stamp

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.stamp)


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# estimation
# --------------------------------------------------------------------------- #
def test_estimate_is_four_characters_per_token():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
def test_reserve_allows_a_call_that_fits():
    budget = TokenBudget(_config(), ledger=InMemoryLedger(), clock=FakeClock())

    budget.reserve(SERVICE_LLM, estimated_input=100, estimated_output=50)


def test_reserve_refuses_when_requests_are_spent():
    budget = TokenBudget(_config(daily_requests=1), ledger=InMemoryLedger(),
                         clock=FakeClock())
    budget.record(SERVICE_LLM, "m", input_tokens=1, output_tokens=1)

    with pytest.raises(BudgetExhaustedError, match="requests"):
        budget.reserve(SERVICE_LLM, estimated_input=1, estimated_output=1)


def test_reserve_refuses_when_input_tokens_are_spent():
    budget = TokenBudget(_config(), ledger=InMemoryLedger(), clock=FakeClock())
    budget.record(SERVICE_LLM, "m", input_tokens=950, output_tokens=0)

    with pytest.raises(BudgetExhaustedError, match="input tokens"):
        budget.reserve(SERVICE_LLM, estimated_input=100, estimated_output=0)


def test_reserve_refuses_when_output_tokens_are_spent():
    budget = TokenBudget(_config(), ledger=InMemoryLedger(), clock=FakeClock())
    budget.record(SERVICE_LLM, "m", input_tokens=0, output_tokens=480)

    with pytest.raises(BudgetExhaustedError, match="output tokens"):
        budget.reserve(SERVICE_LLM, estimated_input=1, estimated_output=100)


def test_refused_reservation_costs_nothing():
    """A reservation that is never spent must not leak into the ledger."""
    ledger = InMemoryLedger()
    budget = TokenBudget(_config(daily_requests=0), ledger=ledger, clock=FakeClock())

    with pytest.raises(BudgetExhaustedError):
        budget.reserve(SERVICE_LLM, estimated_input=1, estimated_output=1)

    assert ledger.spent(budget.today(), SERVICE_LLM).requests == 0


def test_disabled_budget_never_refuses():
    budget = TokenBudget(
        BudgetConfig(enabled=False, llm=ServiceBudget(daily_requests=0)),
        ledger=InMemoryLedger(), clock=FakeClock(),
    )

    budget.reserve(SERVICE_LLM, estimated_input=10**9, estimated_output=10**9)


def test_disabled_budget_records_nothing():
    ledger = InMemoryLedger()
    budget = TokenBudget(BudgetConfig(enabled=False), ledger=ledger, clock=FakeClock())

    budget.record(SERVICE_LLM, "m", input_tokens=100, output_tokens=100)

    assert ledger.spent(budget.today(), SERVICE_LLM).requests == 0


def test_exhaustion_message_says_how_to_raise_the_limit():
    budget = TokenBudget(_config(daily_requests=0), ledger=InMemoryLedger(),
                         clock=FakeClock())

    with pytest.raises(BudgetExhaustedError) as excinfo:
        budget.reserve(SERVICE_LLM, estimated_input=1, estimated_output=1)

    message = str(excinfo.value)
    assert "budget.llm.daily_requests" in message
    assert "--no-budget" in message


# --------------------------------------------------------------------------- #
# accounting
# --------------------------------------------------------------------------- #
def test_spend_is_summed_across_models_within_a_day():
    """Switching model mid-day must not hand the run a fresh allowance."""
    ledger = InMemoryLedger()
    budget = TokenBudget(_config(), ledger=ledger, clock=FakeClock())

    budget.record(SERVICE_LLM, "gemini-2.0-flash", input_tokens=100, output_tokens=10)
    budget.record(SERVICE_LLM, "gemini-2.5-flash", input_tokens=200, output_tokens=20)

    spend = ledger.spent(budget.today(), SERVICE_LLM)
    assert (spend.requests, spend.input_tokens, spend.output_tokens) == (2, 300, 30)


def test_services_are_accounted_separately():
    ledger = InMemoryLedger()
    budget = TokenBudget(_config(), ledger=ledger, clock=FakeClock())

    budget.record(SERVICE_EMBEDDINGS, "e", input_tokens=100, output_tokens=0)

    assert ledger.spent(budget.today(), SERVICE_LLM).input_tokens == 0


def test_a_new_utc_day_starts_a_new_budget():
    clock = FakeClock()
    budget = TokenBudget(_config(daily_requests=1), ledger=InMemoryLedger(), clock=clock)
    budget.record(SERVICE_LLM, "m", input_tokens=1, output_tokens=1)
    with pytest.raises(BudgetExhaustedError):
        budget.reserve(SERVICE_LLM, estimated_input=1, estimated_output=1)

    clock.stamp = "2026-08-21T00:00:01+00:00"

    budget.reserve(SERVICE_LLM, estimated_input=1, estimated_output=1)


def test_the_day_is_utc_not_local():
    """A non-UTC clock is converted, not filed under its own date."""
    budget = TokenBudget(_config(), ledger=InMemoryLedger(),
                         clock=FakeClock("2026-08-21T01:30:00+05:00"))

    assert budget.today() == "2026-08-20"


def test_remaining_reports_what_is_left():
    budget = TokenBudget(_config(), ledger=InMemoryLedger(), clock=FakeClock())
    budget.record(SERVICE_LLM, "m", input_tokens=100, output_tokens=50)

    left = budget.remaining(SERVICE_LLM)

    assert (left.requests, left.input_tokens, left.output_tokens) == (9, 900, 450)


def test_remaining_never_goes_negative():
    budget = TokenBudget(_config(), ledger=InMemoryLedger(), clock=FakeClock())
    budget.record(SERVICE_LLM, "m", input_tokens=5000, output_tokens=5000)

    left = budget.remaining(SERVICE_LLM)

    assert (left.input_tokens, left.output_tokens) == (0, 0)


def test_summary_line_names_both_services_and_the_day():
    budget = TokenBudget(_config(), ledger=InMemoryLedger(), clock=FakeClock())
    budget.record(SERVICE_LLM, "m", input_tokens=100, output_tokens=50)

    line = budget.summary_line()

    assert "llm" in line and "embeddings" in line and "2026-08-20" in line


# --------------------------------------------------------------------------- #
# ledgers
# --------------------------------------------------------------------------- #
def test_sqlite_ledger_matches_the_in_memory_one():
    ledgers = (InMemoryLedger(), DailyLedger(_memory_conn()))
    for ledger in ledgers:
        ledger.add("2026-08-20", SERVICE_LLM, "m", requests=1, input_tokens=10,
                   output_tokens=2)
        ledger.add("2026-08-20", SERVICE_LLM, "m", requests=1, input_tokens=5,
                   output_tokens=1)
        ledger.add("2026-08-21", SERVICE_LLM, "m", requests=1, input_tokens=99,
                   output_tokens=9)

    assert ledgers[0].spent("2026-08-20", SERVICE_LLM) == ledgers[1].spent(
        "2026-08-20", SERVICE_LLM)
    assert ledgers[1].spent("2026-08-20", SERVICE_LLM).input_tokens == 15


def test_sqlite_ledger_persists_across_instances():
    conn = _memory_conn()
    DailyLedger(conn).add("2026-08-20", SERVICE_LLM, "m", requests=1,
                          input_tokens=7, output_tokens=3)

    assert DailyLedger(conn).spent("2026-08-20", SERVICE_LLM).input_tokens == 7


def test_empty_ledger_reports_zero_spend():
    assert DailyLedger(_memory_conn()).spent("2026-08-20", SERVICE_LLM).requests == 0


def test_build_budget_returns_none_when_disabled():
    from config.load import Settings
    from rag.budget import build_budget

    settings = Settings(budget=BudgetConfig(enabled=False))

    assert build_budget(settings) is None


def test_build_budget_uses_the_connection_it_is_given():
    from config.load import Settings
    from rag.budget import build_budget

    conn = _memory_conn()
    budget = build_budget(Settings(), conn=conn)
    budget.record(SERVICE_LLM, "m", input_tokens=11, output_tokens=2)

    row = conn.execute("SELECT input_tokens FROM token_ledger").fetchone()
    assert row["input_tokens"] == 11
