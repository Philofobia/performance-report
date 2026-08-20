"""Unit tests for analysis/llm.py.

The transport is always a fake returning canned strings: no google-genai, no
key, no network (TESTING_PLAN.md §3).
"""
from __future__ import annotations

import json

import pytest

from analysis.llm import (
    GoogleAnalysisClient,
    InvalidModelOutputError,
    LlmPageAnalysis,
    LlmSummary,
    extract_json,
)
from rag.embeddings import QuotaExceededError
from rag.prompt import GroundedPrompt

VALID_PAGE = {
    "summary": "The hero video dominates the LCP path.",
    "findings": [
        {
            "title": "Hero video is the LCP element",
            "detail": "It transfers 2140KB before first paint.",
            "evidence": ["lcp_ms=6200", "hero.mp4 2140KB"],
            "symptom_codes": ["lcp_fail"],
        }
    ],
    "impacts": [{"audience": "ux", "text": "Users stare at an empty hero."}],
    "recommendations": [
        {
            "title": "Replace the autoplay video with a poster image",
            "rationale": "A poster removes 2MB from the critical path.",
            "playbook_source": "images.md",
            "playbook_section": "Serve modern formats",
        }
    ],
}


class FakeTransport:
    """Returns canned responses in order; records what it was asked."""

    def __init__(self, responses, error=None, fail_times=0):
        self._responses = list(responses)
        self._error = error
        self._fail_times = fail_times
        self.calls = []

    def __call__(self, messages, model, *, max_output_tokens=None):
        self.calls.append({
            "messages": messages, "model": model,
            "max_output_tokens": max_output_tokens,
        })
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._error
        return self._responses.pop(0)


def make_client(responses, **kwargs):
    transport_kwargs = {
        key: kwargs.pop(key) for key in ("error", "fail_times") if key in kwargs
    }
    kwargs.setdefault("transport", FakeTransport(responses, **transport_kwargs))
    kwargs.setdefault("sleep", lambda s: None)
    kwargs.setdefault("jitter", lambda: 1.0)
    return GoogleAnalysisClient(model="test-llm", **kwargs)


def a_prompt():
    return GroundedPrompt(system="SYSTEM", user="USER", sources=["images.md"])


# --------------------------------------------------------------------------- #
# extract_json
# --------------------------------------------------------------------------- #
def test_extract_json_passes_through_bare_object():
    assert json.loads(extract_json('{"a": 1}')) == {"a": 1}


def test_extract_json_unwraps_a_fenced_block():
    raw = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
    assert json.loads(extract_json(raw)) == {"a": 1}


def test_extract_json_handles_nested_braces_and_strings():
    raw = 'prefix {"a": {"b": "}"}, "c": 2} suffix'
    assert json.loads(extract_json(raw)) == {"a": {"b": "}"}, "c": 2}


def test_extract_json_raises_when_there_is_no_object():
    with pytest.raises(InvalidModelOutputError):
        extract_json("I would rather not.")


def test_extract_json_raises_on_an_unterminated_object():
    with pytest.raises(InvalidModelOutputError):
        extract_json('{"a": 1')


# --------------------------------------------------------------------------- #
# analyze_page
# --------------------------------------------------------------------------- #
def test_valid_json_is_parsed_into_the_model():
    client = make_client([json.dumps(VALID_PAGE)])
    result = client.analyze_page(a_prompt())
    assert isinstance(result, LlmPageAnalysis)
    assert result.recommendations[0].playbook_source == "images.md"
    assert result.findings[0].symptom_codes == ["lcp_fail"]


def test_prompt_messages_reach_the_transport_unaltered_in_role_order():
    transport = FakeTransport([json.dumps(VALID_PAGE)])
    client = GoogleAnalysisClient(model="test-llm", transport=transport,
                                  sleep=lambda s: None, jitter=lambda: 1.0)
    client.analyze_page(a_prompt())
    messages = transport.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "SYSTEM"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("USER")


def test_malformed_output_is_retried_once_then_succeeds():
    client = make_client(["not json at all", json.dumps(VALID_PAGE)])
    result = client.analyze_page(a_prompt())
    assert result.summary == VALID_PAGE["summary"]


def test_the_retry_turn_tells_the_model_what_broke():
    transport = FakeTransport(["not json at all", json.dumps(VALID_PAGE)])
    client = GoogleAnalysisClient(model="test-llm", transport=transport,
                                  sleep=lambda s: None, jitter=lambda: 1.0)
    client.analyze_page(a_prompt())
    retry_user = transport.calls[1]["messages"][-1]["content"]
    assert "valid JSON" in retry_user


def test_malformed_twice_raises():
    client = make_client(["nope", "still nope"])
    with pytest.raises(InvalidModelOutputError):
        client.analyze_page(a_prompt())


def test_schema_violation_counts_as_malformed():
    bad = {"summary": "x", "findings": [{"title": "t"}],
           "impacts": [{"audience": "marketing", "text": "t"}],
           "recommendations": []}
    client = make_client([json.dumps(bad), json.dumps(bad)])
    with pytest.raises(InvalidModelOutputError):
        client.analyze_page(a_prompt())


def test_quota_error_retries_with_backoff_then_raises():
    transport = FakeTransport([], error=RuntimeError("429 RESOURCE_EXHAUSTED"),
                              fail_times=99)
    slept = []
    client = GoogleAnalysisClient(model="test-llm", transport=transport,
                                  max_retries=3, sleep=slept.append,
                                  jitter=lambda: 1.0)
    with pytest.raises(QuotaExceededError):
        client.analyze_page(a_prompt())
    assert slept == [1.0, 2.0, 4.0]


def test_non_quota_transport_errors_propagate_unwrapped():
    transport = FakeTransport([], error=ValueError("bad argument"), fail_times=1)
    client = GoogleAnalysisClient(model="test-llm", transport=transport,
                                  sleep=lambda s: None, jitter=lambda: 1.0)
    with pytest.raises(ValueError):
        client.analyze_page(a_prompt())


# --------------------------------------------------------------------------- #
# summarize
# --------------------------------------------------------------------------- #
def test_summarize_parses_and_truncates_top_actions():
    payload = {"problem": "p", "key_finding": "k",
               "top_actions": ["a", "b", "c", "d"]}
    client = make_client([json.dumps(payload)])
    result = client.summarize("per-page findings here")
    assert isinstance(result, LlmSummary)
    assert result.top_actions == ["a", "b", "c"]


def test_summarize_rejects_an_empty_action_list():
    payload = {"problem": "p", "key_finding": "k", "top_actions": []}
    client = make_client([json.dumps(payload), json.dumps(payload)])
    with pytest.raises(InvalidModelOutputError):
        client.summarize("findings")


def test_no_api_key_and_no_transport_is_reported_clearly(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = GoogleAnalysisClient(model="test-llm")
    from rag.embeddings import MissingApiKeyError
    with pytest.raises(MissingApiKeyError):
        client.analyze_page(a_prompt())


# --------------------------------------------------------------------------- #
# Budget metering (design spec 2026-08-20)
# --------------------------------------------------------------------------- #
def _budget(**llm):
    from config.load import BudgetConfig, ServiceBudget
    from rag.budget import InMemoryLedger, TokenBudget

    config = BudgetConfig(llm=ServiceBudget(**llm)) if llm else BudgetConfig()
    return TokenBudget(config, ledger=InMemoryLedger())


def _llm_defaults():
    from config.load import BudgetConfig

    return BudgetConfig().llm


def test_generation_records_the_usage_the_api_reports():
    from rag.budget import SERVICE_LLM, Usage

    budget = _budget()
    client = make_client([(json.dumps(VALID_PAGE), Usage(1234, 56))], budget=budget)

    client.analyze_page(a_prompt())

    left = budget.remaining(SERVICE_LLM)
    assert left.input_tokens == _llm_defaults().daily_input_tokens - 1234
    assert left.output_tokens == _llm_defaults().daily_output_tokens - 56


def test_generation_falls_back_to_the_estimate_without_usage():
    """A transport that returns a bare string is still charged for."""
    from rag.budget import SERVICE_LLM

    budget = _budget()
    client = make_client([json.dumps(VALID_PAGE)], budget=budget)

    client.analyze_page(a_prompt())

    left = budget.remaining(SERVICE_LLM)
    assert 0 < left.input_tokens < _llm_defaults().daily_input_tokens
    assert 0 < left.output_tokens < _llm_defaults().daily_output_tokens


def test_the_corrective_retry_costs_a_second_request():
    """The JSON retry is a real call, so it must be a counted one."""
    from rag.budget import SERVICE_LLM

    budget = _budget()
    client = make_client(["not json at all", json.dumps(VALID_PAGE)], budget=budget)

    client.analyze_page(a_prompt())

    assert budget.remaining(SERVICE_LLM).requests == _llm_defaults().daily_requests - 2


def test_quota_retries_are_each_counted():
    from rag.budget import SERVICE_LLM

    budget = _budget()
    client = make_client(
        [json.dumps(VALID_PAGE)], budget=budget,
        error=RuntimeError("429 RESOURCE_EXHAUSTED"), fail_times=2,
    )

    client.analyze_page(a_prompt())

    assert budget.remaining(SERVICE_LLM).requests == _llm_defaults().daily_requests - 3


def test_exhausted_budget_refuses_before_the_transport_runs():
    from rag.budget import BudgetExhaustedError

    transport = FakeTransport([json.dumps(VALID_PAGE)])
    client = make_client([], transport=transport, budget=_budget(daily_requests=0))

    with pytest.raises(BudgetExhaustedError):
        client.analyze_page(a_prompt())

    assert transport.calls == []


def test_the_reservation_assumes_the_worst_case_output():
    """A call that could not be afforded at full length is never started."""
    from rag.budget import BudgetExhaustedError

    transport = FakeTransport([json.dumps(VALID_PAGE)])
    client = make_client(
        [], transport=transport,
        budget=_budget(daily_output_tokens=100, max_output_tokens_per_call=2048),
    )

    with pytest.raises(BudgetExhaustedError, match="output tokens"):
        client.analyze_page(a_prompt())


def test_the_output_cap_reaches_the_transport():
    transport = FakeTransport([json.dumps(VALID_PAGE)])
    client = make_client([], transport=transport, budget=_budget())

    client.analyze_page(a_prompt())

    assert transport.calls[0]["max_output_tokens"] == _llm_defaults(
    ).max_output_tokens_per_call


def test_an_explicit_cap_beats_the_configured_one():
    transport = FakeTransport([json.dumps(VALID_PAGE)])
    client = make_client([], transport=transport, budget=_budget(),
                         max_output_tokens=64)

    client.analyze_page(a_prompt())

    assert transport.calls[0]["max_output_tokens"] == 64


def test_a_transport_that_takes_no_cap_is_called_without_one():
    """Injected two-argument transports keep working unchanged."""
    seen = []

    def transport(messages, model):
        seen.append(model)
        return json.dumps(VALID_PAGE)

    client = make_client([], transport=transport, budget=_budget())

    assert client.analyze_page(a_prompt()).summary
    assert seen == ["test-llm"]


def test_generation_without_a_budget_is_unmetered():
    assert make_client([json.dumps(VALID_PAGE)]).analyze_page(a_prompt()).summary
