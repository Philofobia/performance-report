# Token Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound the Google AI requests and input/output tokens the analysis stage may spend per UTC day, so at least one full report per day completes on the free tier.

**Architecture:** A `TokenBudget` policy object plus a `DailyLedger` (a `token_ledger` table in the existing run store) are injected into the two Google clients, which already accept injected transports. Each client reserves a call's worst-case cost before spending and records actual usage after. Exhaustion raises `BudgetExhaustedError`, which `analyze_page` catches beside the 429 it already handles, degrading that page to the rule-based path.

**Tech Stack:** Python 3.11+, Pydantic v2 (config models), sqlite3 (ledger), pytest (all tests offline — no SDK, no key, no network).

**Spec:** `docs/superpowers/specs/2026-08-20-token-budget-design.md`

## Global Constraints

- **A report always comes out.** Budgeting may change a report's mode; it must never fail a run or raise out of `main()`.
- **No Report schema change.** `meta.degradation_reason` is free text; the committed skeleton baseline enforced by `--skeleton-check` must stay untouched.
- **Tests never touch the network.** Transports are injected fakes; the `google-genai` SDK is never imported in tests.
- **Backwards compatible:** every new parameter defaults to `None`/disabled behaviour, so existing callers and tests keep passing unchanged.
- **The key is never logged**, and no budget message ever includes it.
- Free-tier default limits: `llm` 60 requests / 250000 input / 60000 output tokens, `max_output_tokens_per_call` 2048; `embeddings` 100 requests / 100000 input tokens.
- Service names are exactly `"llm"` and `"embeddings"`.

---

### Task 1: Budget configuration

**Files:**
- Modify: `config/load.py` (add models beside `RagConfig`, mount on `Settings`)
- Modify: `config/settings.yaml` (add the `budget:` block)
- Test: `tests/unit/config_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `config.load.ServiceBudget` (fields `daily_requests: int`, `daily_input_tokens: int`, `daily_output_tokens: int`, `max_output_tokens_per_call: int`), `config.load.BudgetConfig` (fields `enabled: bool`, `llm: ServiceBudget`, `embeddings: ServiceBudget`), and `Settings.budget: BudgetConfig`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/config_test.py`:

```python
def test_budget_defaults_fit_one_report():
    settings = load_settings()
    assert settings.budget.enabled is True
    assert settings.budget.llm.daily_requests == 60
    assert settings.budget.llm.daily_input_tokens == 250000
    assert settings.budget.llm.daily_output_tokens == 60000
    assert settings.budget.llm.max_output_tokens_per_call == 2048
    assert settings.budget.embeddings.daily_requests == 100
    assert settings.budget.embeddings.daily_input_tokens == 100000


def test_budget_rejects_negative_limits():
    from config.load import ServiceBudget

    with pytest.raises(ValidationError):
        ServiceBudget(daily_requests=-1)


def test_budget_zero_is_allowed_and_means_no_budget():
    from config.load import ServiceBudget

    assert ServiceBudget(daily_requests=0).daily_requests == 0
```

Add `from pydantic import ValidationError` to the test module's imports if it is not already there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/config_test.py -k budget -v`
Expected: FAIL — `Settings` has no attribute `budget`, `ImportError` for `ServiceBudget`.

- [ ] **Step 3: Implement the config models**

In `config/load.py`, after `RagConfig`:

```python
class ServiceBudget(BaseModel):
    """Per-UTC-day allowance for one Google service.

    Zero is a legal limit and means "spend nothing on this service" — the
    same outcome as an exhausted budget, which is why it is not a validation
    error.
    """

    daily_requests: int = Field(default=60, ge=0)
    daily_input_tokens: int = Field(default=250_000, ge=0)
    daily_output_tokens: int = Field(default=60_000, ge=0)
    max_output_tokens_per_call: int = Field(default=2048, ge=1)


class BudgetConfig(BaseModel):
    """Free-tier spend control (see docs/superpowers/specs/2026-08-20-token-budget-design.md).

    Google no longer publishes free-tier numbers; these defaults are
    deliberately conservative and every one of them is overridable.
    """

    enabled: bool = True
    llm: ServiceBudget = Field(default_factory=ServiceBudget)
    embeddings: ServiceBudget = Field(
        default_factory=lambda: ServiceBudget(
            daily_requests=100,
            daily_input_tokens=100_000,
            daily_output_tokens=0,
        )
    )
```

Mount it on `Settings` beside `trends`:

```python
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
```

- [ ] **Step 4: Add the settings block**

Append to `config/settings.yaml`:

```yaml
budget:
  # Free-tier spend control. Google no longer publishes free-tier rate limits
  # in the API docs — https://ai.google.dev/gemini-api/docs/rate-limits says
  # only that they are measured as requests/minute, input tokens/minute and
  # requests/day, and points at https://aistudio.google.com/rate-limit for the
  # live values on your key. These numbers are therefore conservative
  # estimates, sized at roughly four times one six-page report, so the first
  # report of a day always fits and a runaway loop cannot eat tomorrow's
  # allowance. Raise them once you have read your own limits in AI Studio.
  enabled: true
  llm:
    daily_requests: 60
    daily_input_tokens: 250000
    daily_output_tokens: 60000
    # Sent to the API as max_output_tokens, and used as the worst-case output
    # cost when deciding whether a call can be afforded.
    max_output_tokens_per_call: 2048
  embeddings:
    daily_requests: 100
    daily_input_tokens: 100000
    # Embeddings return no generated tokens.
    daily_output_tokens: 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/config_test.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add config/load.py config/settings.yaml tests/unit/config_test.py
git commit -m "Add the budget block to settings"
```

---

### Task 2: The ledger and the budget policy

**Files:**
- Create: `rag/budget.py`
- Test: `tests/unit/budget_test.py`

**Interfaces:**
- Consumes: `config.load.BudgetConfig`, `config.load.ServiceBudget` (Task 1); `rag.embeddings.EmbeddingError`.
- Produces:
  - `SERVICE_LLM = "llm"`, `SERVICE_EMBEDDINGS = "embeddings"`
  - `class BudgetExhaustedError(EmbeddingError)`
  - `@dataclass(frozen=True) Spend(requests: int, input_tokens: int, output_tokens: int)`
  - `@dataclass(frozen=True) Usage(input_tokens: int, output_tokens: int)`
  - `estimate_tokens(text: str) -> int`
  - `class InMemoryLedger` and `class DailyLedger`, both with
    `spent(day: str, service: str) -> Spend` and
    `add(day: str, service: str, model: str, *, requests: int, input_tokens: int, output_tokens: int) -> None`
  - `class TokenBudget(config, *, ledger=None, clock=None)` with
    `today() -> str`, `limits_for(service) -> ServiceBudget`,
    `reserve(service, *, estimated_input, estimated_output) -> None`,
    `record(service, model, *, input_tokens, output_tokens) -> None`,
    `remaining(service) -> Spend`, `summary_line() -> str`
  - `build_budget(settings, *, conn=None) -> Optional[TokenBudget]`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/budget_test.py`:

```python
"""Unit tests for rag/budget.py — no SDK, no key, no network."""
from __future__ import annotations

import sqlite3

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

    def __init__(self, stamp="2026-08-20T09:00:00+00:00"):
        self.stamp = stamp

    def __call__(self):
        from datetime import datetime

        return datetime.fromisoformat(self.stamp)


def test_estimate_is_four_characters_per_token():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


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
    ledger = InMemoryLedger()
    budget = TokenBudget(_config(daily_requests=0), ledger=ledger, clock=FakeClock())
    with pytest.raises(BudgetExhaustedError):
        budget.reserve(SERVICE_LLM, estimated_input=1, estimated_output=1)
    assert ledger.spent(budget.today(), SERVICE_LLM).requests == 0


def test_disabled_budget_never_refuses():
    budget = TokenBudget(BudgetConfig(enabled=False, llm=ServiceBudget(daily_requests=0)),
                         ledger=InMemoryLedger(), clock=FakeClock())
    budget.reserve(SERVICE_LLM, estimated_input=10**9, estimated_output=10**9)


def test_spend_is_summed_across_models_within_a_day():
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


def test_remaining_reports_what_is_left():
    budget = TokenBudget(_config(), ledger=InMemoryLedger(), clock=FakeClock())
    budget.record(SERVICE_LLM, "m", input_tokens=100, output_tokens=50)
    left = budget.remaining(SERVICE_LLM)
    assert (left.requests, left.input_tokens, left.output_tokens) == (9, 900, 450)


def test_summary_line_names_both_services_and_the_day():
    budget = TokenBudget(_config(), ledger=InMemoryLedger(), clock=FakeClock())
    budget.record(SERVICE_LLM, "m", input_tokens=100, output_tokens=50)
    line = budget.summary_line()
    assert "llm" in line and "embeddings" in line and "2026-08-20" in line


def test_sqlite_ledger_matches_the_in_memory_one():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pair = (InMemoryLedger(), DailyLedger(conn))
    for ledger in pair:
        ledger.add("2026-08-20", SERVICE_LLM, "m", requests=1, input_tokens=10, output_tokens=2)
        ledger.add("2026-08-20", SERVICE_LLM, "m", requests=1, input_tokens=5, output_tokens=1)
        ledger.add("2026-08-21", SERVICE_LLM, "m", requests=1, input_tokens=99, output_tokens=9)
    assert pair[0].spent("2026-08-20", SERVICE_LLM) == pair[1].spent("2026-08-20", SERVICE_LLM)
    assert pair[1].spent("2026-08-20", SERVICE_LLM).input_tokens == 15


def test_sqlite_ledger_survives_reconnection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    DailyLedger(conn).add("2026-08-20", SERVICE_LLM, "m", requests=1,
                          input_tokens=7, output_tokens=3)
    assert DailyLedger(conn).spent("2026-08-20", SERVICE_LLM).input_tokens == 7


def test_exhaustion_message_says_how_to_raise_the_limit():
    budget = TokenBudget(_config(daily_requests=0), ledger=InMemoryLedger(), clock=FakeClock())
    with pytest.raises(BudgetExhaustedError) as excinfo:
        budget.reserve(SERVICE_LLM, estimated_input=1, estimated_output=1)
    message = str(excinfo.value)
    assert "budget.llm.daily_requests" in message
    assert "--no-budget" in message
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/budget_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rag.budget'`.

- [ ] **Step 3: Implement `rag/budget.py`**

```python
"""Free-tier spend control: a per-UTC-day ledger and the policy over it.

The pipeline's only defence against the free tier used to be reactive — retry
the 429, then degrade. That protects the *last* run of a day, not the first.
This module makes the cost of a call knowable before it is made, so a campaign
can be stopped from spending quota the day's first report needs.

Two objects, kept apart on purpose: :class:`DailyLedger` is storage and knows
nothing about limits; :class:`TokenBudget` is policy and can be tested against
:class:`InMemoryLedger` without SQLite.

Counting is local (``len(text) / 4``) rather than Google's ``countTokens``
endpoint, because that endpoint is itself a request against the same free-tier
request limit — metering with it would spend the thing being rationed. The
estimate only decides whether to *start* a call; the ledger is corrected from
the response afterwards, so estimation error never compounds across a day.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple

from config.load import BudgetConfig, ServiceBudget
from rag.embeddings import EmbeddingError

SERVICE_LLM = "llm"
SERVICE_EMBEDDINGS = "embeddings"

#: Rough characters-per-token ratio for English prose and JSON alike.
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

    Subclasses ``EmbeddingError`` so the pipeline's existing degradation paths
    already treat it as "no model available" rather than letting it escape.
    """


@dataclass(frozen=True)
class Spend:
    """Requests and tokens, used both for spend and for what is left."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Usage:
    """Actual token counts reported by the API for one call."""

    input_tokens: int = 0
    output_tokens: int = 0


def estimate_tokens(text: str) -> int:
    """Rough token count for deciding whether a call can be afforded."""
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
    """SQLite-backed ledger, living in the run store beside the runs it paid for."""

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
        return Spend(int(row["r"]), int(row["i"]), int(row["o"]))

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
    """Compact token counts: 38200 -> '38.2k'."""
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


class TokenBudget:
    """Decides whether a call may be made, and records what it cost.

    ``clock`` is injected so tests can cross UTC midnight deterministically,
    the same way ``jitter`` is injected into the backoff schedule.
    """

    def __init__(
        self,
        config: BudgetConfig,
        *,
        ledger: Optional[object] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._config = config
        self._ledger = ledger if ledger is not None else InMemoryLedger()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- state ------------------------------------------------------------- #
    def today(self) -> str:
        """The current UTC day, the key every ledger row is filed under."""
        return self._clock().astimezone(timezone.utc).date().isoformat()

    def limits_for(self, service: str) -> ServiceBudget:
        return (
            self._config.llm if service == SERVICE_LLM else self._config.embeddings
        )

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

        Nothing is written here: a refused call costs nothing, and a
        reservation that is never spent must not leak into tomorrow.
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
                    f"(UTC): {label} {used}/{cap}, this call needs {wanted} more. "
                    f"Raise budget.{service}.{key} in config/settings.yaml, "
                    f"pass --no-budget to spend freely, or wait for the next "
                    f"UTC day."
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


def build_budget(settings, *, conn: Optional[sqlite3.Connection] = None):
    """Build the budget for a run, or ``None`` when budgeting is off.

    A ledger that cannot be created is not fatal: bookkeeping must never cost
    a report, so the caller falls back to counting in memory for this run.
    """
    if not settings.budget.enabled:
        return None
    ledger: Optional[object] = None
    if conn is not None:
        try:
            ledger = DailyLedger(conn)
        except sqlite3.Error:
            ledger = None
    return TokenBudget(settings.budget, ledger=ledger)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/budget_test.py -v`
Expected: PASS (16 tests).

- [ ] **Step 5: Commit**

```bash
git add rag/budget.py tests/unit/budget_test.py
git commit -m "Add the daily token ledger and budget policy"
```

---

### Task 3: Budget the embeddings client

**Files:**
- Modify: `rag/embeddings.py` (`GoogleEmbeddingClient.__init__`, `_call`)
- Test: `tests/unit/rag_test.py`

**Interfaces:**
- Consumes: `TokenBudget`, `SERVICE_EMBEDDINGS`, `estimate_tokens`, `BudgetExhaustedError` (Task 2).
- Produces: `GoogleEmbeddingClient(..., budget: Optional[TokenBudget] = None)`. One reservation and one ledger record per batch; output tokens always 0.

**Note on import order:** `rag/budget.py` imports from `rag/embeddings.py`, so `rag/embeddings.py` must import budget names *inside* `_call` (a local import), exactly as it already imports numpy locally. A module-level import here is a circular import and will fail at collection.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/rag_test.py`:

```python
def test_embedding_batch_spends_budget_once():
    from config.load import BudgetConfig
    from rag.budget import SERVICE_EMBEDDINGS, InMemoryLedger, TokenBudget

    budget = TokenBudget(BudgetConfig(), ledger=InMemoryLedger())
    client = GoogleEmbeddingClient(transport=FakeTransport(), budget=budget)
    client.embed(["alpha", "beta"])
    spend = budget.remaining(SERVICE_EMBEDDINGS)
    assert spend.requests == BudgetConfig().embeddings.daily_requests - 1


def test_cached_text_costs_no_budget(tmp_path):
    from config.load import BudgetConfig
    from rag.budget import SERVICE_EMBEDDINGS, InMemoryLedger, TokenBudget

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    budget = TokenBudget(BudgetConfig(), ledger=InMemoryLedger())
    cache = EmbeddingCache(conn)
    client = GoogleEmbeddingClient(transport=FakeTransport(), cache=cache, budget=budget)
    client.embed(["alpha"])
    before = budget.remaining(SERVICE_EMBEDDINGS).requests
    client.embed(["alpha"])
    assert budget.remaining(SERVICE_EMBEDDINGS).requests == before


def test_exhausted_embedding_budget_raises_before_the_call():
    from config.load import BudgetConfig, ServiceBudget
    from rag.budget import BudgetExhaustedError, InMemoryLedger, TokenBudget

    budget = TokenBudget(
        BudgetConfig(embeddings=ServiceBudget(daily_requests=0)),
        ledger=InMemoryLedger(),
    )
    transport = FakeTransport()
    client = GoogleEmbeddingClient(transport=transport, budget=budget)
    with pytest.raises(BudgetExhaustedError):
        client.embed(["alpha"])
    assert transport.calls == []
```

Match the existing module's fake: reuse whichever fake transport class `tests/unit/rag_test.py` already defines (it records calls), and add a `calls` list to it if it does not have one. Add `import sqlite3` to the test module if absent.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/rag_test.py -k budget -v`
Expected: FAIL — `GoogleEmbeddingClient() got an unexpected keyword argument 'budget'`.

- [ ] **Step 3: Implement**

In `GoogleEmbeddingClient.__init__`, add the parameter and store it:

```python
        budget: Optional[Any] = None,
```
```python
        self._budget = budget
```

Replace the body of `_call` with:

```python
    def _call(self, texts: Sequence[str], task_type: str) -> List[List[float]]:
        """One transport call wrapped in retry/backoff on quota errors.

        The budget is reserved *before* the call and recorded after it, so a
        refused batch costs nothing and a retried one is counted once per
        request actually made.
        """
        if self._transport is None:
            self._transport = self._build_default_transport()

        # Local import: rag.budget imports this module.
        from rag.budget import SERVICE_EMBEDDINGS, estimate_tokens

        tokens = sum(estimate_tokens(text) for text in texts)
        if self._budget is not None:
            self._budget.reserve(
                SERVICE_EMBEDDINGS, estimated_input=tokens, estimated_output=0
            )

        vectors = call_with_quota_backoff(
            lambda: self._transport(texts, self.model, task_type),
            max_retries=self._max_retries,
            sleep=self._sleep,
            jitter=self._jitter,
            exhausted_message=(
                f"Google AI quota exhausted after {self._max_retries} retries. "
                "The free tier limits requests per minute; wait for the window "
                "to reset or reduce the corpus size."
            ),
        )
        if self._budget is not None:
            # The embed endpoint reports no usage metadata, so the estimate is
            # the best count available — stated here so nobody mistakes it for
            # a measured figure.
            self._budget.record(
                SERVICE_EMBEDDINGS, self.model,
                input_tokens=tokens, output_tokens=0,
            )
        return vectors
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/rag_test.py -v`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add rag/embeddings.py tests/unit/rag_test.py
git commit -m "Meter embedding batches against the daily budget"
```

---

### Task 4: Budget the generation client and cap output tokens

**Files:**
- Modify: `analysis/llm.py` (`GoogleAnalysisClient.__init__`, `_build_default_transport`, `_call`)
- Test: `tests/unit/analysis_llm_test.py`

**Interfaces:**
- Consumes: `TokenBudget`, `SERVICE_LLM`, `estimate_tokens`, `Usage` (Task 2).
- Produces: `GoogleAnalysisClient(..., budget: Optional[TokenBudget] = None)`. Every `_call` — including the corrective JSON retry — reserves and records once. Transports may return `str` or `(str, Usage)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/analysis_llm_test.py`:

```python
def _budget(**llm):
    from config.load import BudgetConfig, ServiceBudget
    from rag.budget import InMemoryLedger, TokenBudget

    return TokenBudget(BudgetConfig(llm=ServiceBudget(**llm)) if llm else BudgetConfig(),
                       ledger=InMemoryLedger())


def test_generation_records_actual_usage_when_reported():
    from rag.budget import SERVICE_LLM, Usage

    budget = _budget()
    transport = FakeTransport([(json.dumps(VALID_PAGE), Usage(1234, 56))])
    client = GoogleAnalysisClient(transport=transport, budget=budget)
    client.analyze_page(GroundedPrompt(system="s", user="u"))
    left = budget.remaining(SERVICE_LLM)
    from config.load import BudgetConfig

    defaults = BudgetConfig().llm
    assert left.input_tokens == defaults.daily_input_tokens - 1234
    assert left.output_tokens == defaults.daily_output_tokens - 56


def test_generation_falls_back_to_the_estimate_without_usage():
    from rag.budget import SERVICE_LLM

    budget = _budget()
    client = GoogleAnalysisClient(
        transport=FakeTransport([json.dumps(VALID_PAGE)]), budget=budget)
    client.analyze_page(GroundedPrompt(system="s", user="u"))
    assert budget.remaining(SERVICE_LLM).input_tokens < BudgetConfig().llm.daily_input_tokens
```

(Add `from config.load import BudgetConfig` at the top of the second test if the linter prefers it; the import inside the first test is intentional to keep the module's existing import block untouched.)

```python
def test_the_corrective_retry_costs_a_second_request():
    from rag.budget import SERVICE_LLM

    budget = _budget()
    client = GoogleAnalysisClient(
        transport=FakeTransport(["not json at all", json.dumps(VALID_PAGE)]),
        budget=budget,
    )
    client.analyze_page(GroundedPrompt(system="s", user="u"))
    from config.load import BudgetConfig

    assert budget.remaining(SERVICE_LLM).requests == BudgetConfig().llm.daily_requests - 2


def test_exhausted_budget_refuses_before_the_transport_runs():
    from rag.budget import BudgetExhaustedError

    budget = _budget(daily_requests=0)
    transport = FakeTransport([json.dumps(VALID_PAGE)])
    client = GoogleAnalysisClient(transport=transport, budget=budget)
    with pytest.raises(BudgetExhaustedError):
        client.analyze_page(GroundedPrompt(system="s", user="u"))
    assert transport.calls == []


def test_max_output_tokens_reaches_the_generation_config():
    captured = {}

    def transport(messages, model, *, max_output_tokens=None):
        captured["cap"] = max_output_tokens
        return json.dumps(VALID_PAGE)

    client = GoogleAnalysisClient(transport=transport, budget=_budget())
    client.analyze_page(GroundedPrompt(system="s", user="u"))
    assert captured["cap"] == 2048


def test_two_argument_transports_still_work():
    client = GoogleAnalysisClient(transport=FakeTransport([json.dumps(VALID_PAGE)]))
    assert client.analyze_page(GroundedPrompt(system="s", user="u")).summary
```

`FakeTransport` in this module must be extended to: record each call in a `self.calls` list, and return whatever the canned response is (string *or* `(string, Usage)` tuple) unchanged. Check its current constructor before editing — it already takes `responses`, `error` and `fail_times`. Construct `GroundedPrompt` the way the existing tests in this file do; copy that call, do not invent field names.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/analysis_llm_test.py -k "budget or usage or retry_costs or max_output" -v`
Expected: FAIL — unexpected keyword argument `budget`.

- [ ] **Step 3: Implement**

In `GoogleAnalysisClient.__init__` add:

```python
        budget: Optional[Any] = None,
        max_output_tokens: Optional[int] = None,
```
```python
        self._budget = budget
        self._max_output_tokens = max_output_tokens
```

Add a helper on the class that resolves the per-call output cap — the explicit
argument wins, then the budget's configured cap, then no cap:

```python
    def _output_cap(self) -> Optional[int]:
        """The per-call output ceiling: explicit, else the budget's, else none."""
        if self._max_output_tokens is not None:
            return self._max_output_tokens
        if self._budget is not None:
            from rag.budget import SERVICE_LLM

            return self._budget.limits_for(SERVICE_LLM).max_output_tokens_per_call
        return None
```

Teach the real transport to accept the cap and return usage:

```python
        def transport(messages, model, *, max_output_tokens=None):
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            contents = [m["content"] for m in messages if m["role"] != "system"]
            config: Dict[str, Any] = {
                "system_instruction": system,
                "temperature": 0,
                "response_mime_type": "application/json",
            }
            if max_output_tokens:
                config["max_output_tokens"] = max_output_tokens
            response = client.models.generate_content(
                model=model, contents=contents, config=config,
            )
            from rag.budget import Usage

            meta = getattr(response, "usage_metadata", None)
            usage = Usage(
                int(getattr(meta, "prompt_token_count", 0) or 0),
                int(getattr(meta, "candidates_token_count", 0) or 0),
            ) if meta is not None else None
            return (response.text or "", usage) if usage else (response.text or "")
```

Replace `_call`:

```python
    def _call(self, messages: List[Dict[str, str]]) -> str:
        """One generation call: reserved, retried on quota only, then recorded.

        Reserving here rather than in ``analyze_page`` is deliberate — this is
        the one place every request passes through, including the corrective
        JSON retry, which is a second real call to the API.
        """
        if self._transport is None:
            self._transport = self._build_default_transport()

        from rag.budget import SERVICE_LLM, Usage, estimate_tokens

        estimated_input = sum(estimate_tokens(m["content"]) for m in messages)
        cap = self._output_cap()
        if self._budget is not None:
            self._budget.reserve(
                SERVICE_LLM,
                estimated_input=estimated_input,
                # Worst case: a reply that runs to the ceiling. Never start a
                # call that could not be afforded if the model used it all.
                estimated_output=cap or 0,
            )

        def _invoke():
            if cap is None:
                return self._transport(messages, self.model)
            try:
                return self._transport(messages, self.model, max_output_tokens=cap)
            except TypeError:
                # Injected transports predating the cap take two arguments.
                return self._transport(messages, self.model)

        result = call_with_quota_backoff(
            _invoke,
            max_retries=self._max_retries,
            sleep=self._sleep,
            jitter=self._jitter,
            exhausted_message=(
                f"Google AI quota exhausted after {self._max_retries} retries. "
                "The free tier limits requests per minute; wait for the window "
                "to reset or re-run with --no-llm."
            ),
        )

        text, usage = result if isinstance(result, tuple) else (result, None)
        if self._budget is not None:
            if usage is None:
                usage = Usage(estimated_input, estimate_tokens(text))
            self._budget.record(
                SERVICE_LLM, self.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        return text
```

Add `Any` to the module's `typing` import if it is not already there.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/analysis_llm_test.py -v`
Expected: PASS, including every pre-existing test.

- [ ] **Step 5: Commit**

```bash
git add analysis/llm.py tests/unit/analysis_llm_test.py
git commit -m "Meter generation calls and cap output tokens"
```

---

### Task 5: Degrade a page when its budget is gone

**Files:**
- Modify: `analysis/findings.py:428-440` (the `except` ladder in `analyze_page`)
- Test: `tests/unit/findings_test.py`, `tests/integration/analysis_pipeline_test.py`

**Interfaces:**
- Consumes: `BudgetExhaustedError` (Task 2).
- Produces: `degradation_reason == "budget_exhausted"` on any page whose model call was refused by the budget.

**Ordering matters:** `BudgetExhaustedError` subclasses `EmbeddingError`, so its clause must come *before* the existing `except (InvalidModelOutputError, AnalysisError, EmbeddingError)` clause, or it will be swallowed and mislabelled `invalid_model_output`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/findings_test.py` (copy the fixture/helper style already used by `test_missing_key_degrades_to_rules` in that file — reuse its runs and symptoms rather than inventing new ones):

```python
def test_budget_exhaustion_degrades_the_page_with_its_own_reason():
    from rag.budget import BudgetExhaustedError

    class RefusingClient:
        model = "gemini-2.0-flash"

        def analyze_page(self, prompt):
            raise BudgetExhaustedError("spent")

    result = analyze_page(_runs(), hits=[], symptoms=[], client=RefusingClient())
    assert result.mode == "rule_based"
    assert result.degradation_reason == "budget_exhausted"
```

And in `tests/integration/analysis_pipeline_test.py`, following the shape of the existing `no_api_key` test:

```python
def test_report_still_written_when_the_budget_runs_out(tmp_path):
    """One page's worth of budget: page one keeps its prose, the rest degrade."""
    from config.load import BudgetConfig, ServiceBudget
    from rag.budget import InMemoryLedger, TokenBudget

    budget = TokenBudget(
        BudgetConfig(llm=ServiceBudget(daily_requests=1)), ledger=InMemoryLedger()
    )
    llm_client = GoogleAnalysisClient(
        transport=FakeTransport([json.dumps(VALID_PAGE)] * 4), budget=budget
    )
    report = run_analysis(_two_page_runs(), llm_client=llm_client, settings=load_settings())
    assert report.meta.analysis_mode in {"mixed", "rule_based"}
    assert report.meta.degradation_reason == "budget_exhausted"
```

Use the module's existing helpers for the runs and the fake transport; `_two_page_runs()` stands for whatever that file already uses to build a two-page campaign. If `analysis_mode` for a partially degraded campaign is not `"mixed"`, assert whatever `build_report` actually produces — read `analysis/reportmodel.py:515-525` and match it rather than changing production behaviour to fit the test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/findings_test.py -k budget tests/integration/analysis_pipeline_test.py -k budget -v`
Expected: FAIL — `degradation_reason == "invalid_model_output"`.

- [ ] **Step 3: Implement**

In `analysis/findings.py`, extend the import inside `analyze_page`:

```python
    from rag.budget import BudgetExhaustedError
```

and insert this clause *above* the existing `except (InvalidModelOutputError, ...)`:

```python
    except BudgetExhaustedError:
        # Ahead of the EmbeddingError clause on purpose: this subclasses it,
        # and "we chose not to spend" must not be reported as a bad response.
        return _rule_based_page(
            ordered_runs, primary, list(symptoms), corpus, "budget_exhausted"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/findings_test.py tests/integration/analysis_pipeline_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add analysis/findings.py tests/unit/findings_test.py tests/integration/analysis_pipeline_test.py
git commit -m "Degrade a page to rules when its budget is spent"
```

---

### Task 6: Wire the budget into the analysis CLI

**Files:**
- Modify: `analysis/__main__.py` (`_build_live_clients`, `_build_parser`, `main`)
- Test: `tests/unit/cli_test.py` or `tests/integration/analysis_pipeline_test.py` (put CLI-flag tests where that file already tests `--no-llm`)

**Interfaces:**
- Consumes: `build_budget`, `TokenBudget` (Task 2); the budgeted clients (Tasks 3–4).
- Produces: flags `--no-budget`, `--daily-requests`, `--daily-input-tokens`, `--daily-output-tokens`, `--max-output-tokens`, `--budget-status`; a `budget:` summary line on stderr after a budgeted run.

- [ ] **Step 1: Write the failing tests**

```python
def test_budget_status_prints_without_calling_anything(capsys, tmp_path):
    from analysis.__main__ import main

    assert main(["--budget-status"]) == 0
    out = capsys.readouterr().out
    assert "budget:" in out and "llm" in out


def test_no_budget_disables_accounting(capsys, tmp_path):
    """--no-budget must not print the summary line."""
    from analysis.__main__ import main

    main(["--budget-status", "--no-budget"])
    assert "budget: disabled" in capsys.readouterr().out


def test_cli_overrides_reach_the_budget():
    from analysis.__main__ import _budget_from_args, _build_parser
    from config.load import load_settings

    args = _build_parser().parse_args(["--daily-requests", "7",
                                       "--daily-input-tokens", "8",
                                       "--daily-output-tokens", "9",
                                       "--max-output-tokens", "10"])
    budget = _budget_from_args(args, load_settings())
    limits = budget.limits_for("llm")
    assert (limits.daily_requests, limits.daily_input_tokens,
            limits.daily_output_tokens, limits.max_output_tokens_per_call) == (7, 8, 9, 10)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/integration/analysis_pipeline_test.py -k budget -v`
Expected: FAIL — unrecognised arguments / `ImportError: _budget_from_args`.

- [ ] **Step 3: Implement**

Add the flags in `_build_parser`:

```python
    p.add_argument("--no-budget", action="store_true",
                   help="Spend freely: make no daily token/request budget checks.")
    p.add_argument("--budget-status", action="store_true",
                   help="Print today's token spend and exit, making no API calls.")
    p.add_argument("--daily-requests", type=int, default=None,
                   help="Override budget.llm.daily_requests for this run.")
    p.add_argument("--daily-input-tokens", type=int, default=None,
                   help="Override budget.llm.daily_input_tokens for this run.")
    p.add_argument("--daily-output-tokens", type=int, default=None,
                   help="Override budget.llm.daily_output_tokens for this run.")
    p.add_argument("--max-output-tokens", type=int, default=None,
                   help="Override budget.llm.max_output_tokens_per_call.")
```

Add the builder, which applies the overrides to a *copy* of the settings' budget
so nothing mutates the loaded configuration:

```python
def _budget_from_args(args, settings):
    """Build the run's budget from settings plus command-line overrides."""
    from rag.budget import build_budget

    if getattr(args, "no_budget", False):
        return None

    overrides = {
        "daily_requests": args.daily_requests,
        "daily_input_tokens": args.daily_input_tokens,
        "daily_output_tokens": args.daily_output_tokens,
        "max_output_tokens_per_call": args.max_output_tokens,
    }
    supplied = {k: v for k, v in overrides.items() if v is not None}
    if supplied:
        settings = settings.model_copy(deep=True)
        settings.budget.llm = settings.budget.llm.model_copy(update=supplied)

    from store import sql

    conn = None
    try:
        conn = sql.connect(settings.storage.sqlite_path)
    except Exception as exc:  # bookkeeping must never cost a report
        print(f"Token ledger unavailable, counting in memory only: {exc}",
              file=sys.stderr)
    return build_budget(settings, conn=conn)
```

Give `_build_live_clients` the budget and pass it to both clients:

```python
def _build_live_clients(settings, budget=None) -> tuple:
```
```python
    embed_client = GoogleEmbeddingClient(model=settings.models.embeddings, budget=budget)
    llm_client = GoogleAnalysisClient(model=settings.models.llm, budget=budget)
```

In `main`, after `settings = load_settings()`:

```python
    budget = _budget_from_args(args, settings)

    if args.budget_status:
        print("budget: disabled" if budget is None else budget.summary_line())
        return 0
```

Pass it through the client build:

```python
    if not args.no_llm:
        store, embed_client, llm_client = _build_live_clients(settings, budget)
```

And print the spend once the report is written, just before the final `return 0`:

```python
    if budget is not None and not args.no_llm:
        print(budget.summary_line(), file=sys.stderr)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: PASS — the whole suite, not just the new tests.

- [ ] **Step 5: Verify the drift guard still passes**

Run: `python -m report --skeleton-check` against a previously generated report if one exists locally; otherwise confirm `tests/unit/skeleton_test.py` passes.
Expected: no skeleton drift — this feature changes no report section.

- [ ] **Step 6: Commit**

```bash
git add analysis/__main__.py tests/
git commit -m "Expose the token budget on the analyze command"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md` (status section + a "Token budget" subsection + the analyze flag table if one exists)
- Modify: `docs/PROJECT_SPEC.md` (settings reference)
- Test: `python -m pytest tests/ -v` (docs tests, if any, live in `tests/unit/ci_workflow_test.py`)

**Interfaces:**
- Consumes: everything above. Produces no code.

- [ ] **Step 1: Update the README**

In "Where the project is", add to the working-today list: `**a per-UTC-day token and request budget over the Google free tier, with the analysis stage degrading to rules rather than overspending**`.

Add a subsection near the configuration documentation:

````markdown
### Token budget

The free tier is rationed, and Google no longer publishes the numbers — the
[rate-limits page](https://ai.google.dev/gemini-api/docs/rate-limits) says only
that limits are counted as requests/minute, input tokens/minute and
requests/day, and points at [AI Studio](https://aistudio.google.com/rate-limit)
for the values on your own key.

So the analysis stage keeps its own ledger. Every generation and embedding call
is reserved against a per-UTC-day allowance before it is made and recorded from
the response afterwards, in a `token_ledger` table in the run store. When the
day's allowance is gone, the remaining pages degrade to the rule-based path and
the report says `budget_exhausted` — a report always comes out.

The defaults in `config/settings.yaml` are sized at roughly four times one
six-page report, so the first report of a day always fits:

| | requests/day | input tokens/day | output tokens/day |
| --- | --- | --- | --- |
| `llm` | 60 | 250,000 | 60,000 |
| `embeddings` | 100 | 100,000 | — |

Per run:

```bash
python -m cli analyze --budget-status          # today's spend; makes no API call
python -m cli analyze --daily-input-tokens 500000
python -m cli analyze --no-budget              # spend freely
```
````

- [ ] **Step 2: Update PROJECT_SPEC.md**

Add the `budget:` block to the settings reference section, matching the
formatting used for `trends:` and `rag:` there.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/PROJECT_SPEC.md
git commit -m "Document the token budget"
```

---

## Self-Review

- **Spec coverage:** §1 config → Task 1. §2 `rag/budget.py` → Task 2. §3 integration: embeddings → Task 3, llm → Task 4, findings → Task 5, `__main__` → Task 6; reportmodel needs no change (verified: `degradation_reason` is `Optional[str]`, free text). §4 CLI → Task 6. §5 failure table → Tasks 5 (page degradation) and 6 (ledger-unwritable warning); the `persist_findings` row needs no code, as its `except Exception` already covers it. §6 testing → tests in every task. §7 docs → Task 7.
- **Placeholder scan:** every code step carries real code. The two places that say "match the file's existing helper" (`FakeTransport`, `_two_page_runs`) are instructions to reuse a specific existing thing, not deferred decisions.
- **Type consistency:** `Spend` is used for both spend and remaining (checked: `remaining()` returns `Spend`, and the tests read `.requests`/`.input_tokens`/`.output_tokens`). `Usage` carries `(input_tokens, output_tokens)` positionally in Task 4's test and by keyword in the transport. Service names are the two constants everywhere. `reserve`/`record`/`remaining`/`limits_for`/`summary_line`/`today` are spelled identically in Tasks 2, 3, 4 and 6.
