"""Google generation client with a strict JSON output contract.

The model's job in this system is narrow: write prose, and say *which*
playbook justifies each recommendation. It is given no field in which to put
a number — cheaper than deleting hallucinated magnitudes after the fact, and
the reason ``estimator.py`` can stay the sole source of every figure in the
report (§11).

Everything else mirrors ``rag/embeddings.py`` so the codebase has one shape
for "calls Google": injected transport, lazy SDK import, quota backoff with
jitter, typed errors, and a key that is resolved in exactly one place and
never logged.

One retry on malformed output, with the parse error fed back as a correction
turn. Models usually fix their own JSON on the second attempt; a third try
mostly buys latency. After that the caller degrades to the rule-based path.
"""
from __future__ import annotations

import inspect
import json
import random
import time
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from rag.embeddings import (
    QuotaExceededError,
    call_with_quota_backoff,
    resolve_api_key,
)
from rag.prompt import GroundedPrompt

DEFAULT_MAX_RETRIES = 3

JSON_INSTRUCTION = """

# OUTPUT FORMAT
Reply with a single JSON object and nothing else - no prose before or after,
no code fence. Shape:

{
  "summary": "one paragraph on what is wrong with this page",
  "findings": [
    {"title": "...", "detail": "...", "evidence": ["metric=value"],
     "symptom_codes": ["lcp_fail"]}
  ],
  "impacts": [{"audience": "ux|seo|business", "text": "..."}],
  "recommendations": [
    {"title": "...", "rationale": "...",
     "playbook_source": "<the source name of a playbook shown above>",
     "playbook_section": "<the heading you used>"}
  ]
}

Do not include numeric estimates of improvement anywhere. Magnitudes are
computed from the playbooks by this system, not by you.\
"""

SUMMARY_SYSTEM = """\
You are a web performance analyst writing the executive summary of a report.
You are given findings this system already produced. Synthesise them. Do not
introduce new claims, new metrics, or numeric improvement estimates.

Reply with a single JSON object and nothing else:

{"problem": "...", "key_finding": "...", "top_actions": ["...", "...", "..."]}\
"""


class AnalysisError(Exception):
    """Base class for analysis failures."""


class LlmUnavailableError(AnalysisError):
    """No usable model client (SDK missing, or generation unreachable)."""


class InvalidModelOutputError(AnalysisError):
    """The model did not return output matching the required JSON contract."""


# --------------------------------------------------------------------------- #
# Output models — the contract. No numeric fields, by design.
# --------------------------------------------------------------------------- #
class LlmFinding(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=2000)
    evidence: List[str] = Field(default_factory=list, max_length=20)
    symptom_codes: List[str] = Field(default_factory=list, max_length=20)


class LlmImpact(BaseModel):
    audience: Literal["ux", "seo", "business"]
    text: str = Field(min_length=1, max_length=1000)


class LlmRecommendation(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(default="", max_length=2000)
    playbook_source: str = Field(min_length=1, max_length=200)
    playbook_section: str = Field(default="", max_length=200)


class LlmPageAnalysis(BaseModel):
    summary: str = Field(default="", max_length=4000)
    findings: List[LlmFinding] = Field(default_factory=list, max_length=20)
    impacts: List[LlmImpact] = Field(default_factory=list, max_length=20)
    recommendations: List[LlmRecommendation] = Field(
        default_factory=list, max_length=20
    )


class LlmSummary(BaseModel):
    problem: str = Field(min_length=1, max_length=2000)
    key_finding: str = Field(min_length=1, max_length=2000)
    top_actions: List[str] = Field(min_length=1, max_length=3)

    @field_validator("top_actions", mode="before")
    @classmethod
    def _truncate(cls, value: Any) -> Any:
        """Take the first three rather than rejecting an over-long list.

        A model that offers five good actions has not failed the contract; it
        has been generous. Truncating is the graceful read.
        """
        return value[:3] if isinstance(value, list) else value


def extract_json(text: str) -> str:
    """Pull the outermost balanced JSON object out of a model response.

    Models wrap JSON in fences and apologies no matter how firmly the prompt
    says not to. Brace-matching is string-aware so a ``}`` inside a value does
    not end the object early.
    """
    start = text.find("{")
    if start == -1:
        raise InvalidModelOutputError("Model response contained no JSON object.")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise InvalidModelOutputError("Model response had an unterminated JSON object.")


class GoogleAnalysisClient:
    """Google generation with retry/backoff and a validated JSON contract.

    ``transport`` is the injection point: ``(messages, model) -> str``. When
    omitted a real ``google-genai`` client is built lazily, so importing this
    module never requires the SDK or a key.
    """

    def __init__(
        self,
        *,
        model: str = "gemini-2.0-flash",
        api_key: Optional[str] = None,
        transport: Optional[Callable[..., Any]] = None,
        budget: Optional[Any] = None,
        max_output_tokens: Optional[int] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.model = model
        self._explicit_key = api_key
        self._transport = transport
        self._budget = budget
        self._max_output_tokens = max_output_tokens
        #: Resolved once, on first use: whether the transport takes a cap.
        self._takes_cap: Optional[bool] = None
        self._max_retries = max(0, max_retries)
        self._sleep = sleep
        self._jitter = jitter

    # -- transport --------------------------------------------------------- #
    def _build_default_transport(self):
        """Lazily construct a real google-genai transport."""
        key = resolve_api_key(self._explicit_key)
        try:
            from google import genai  # lazy: tests never need the SDK
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise LlmUnavailableError(
                "google-genai is not installed. Install it with "
                "`pip install -r requirements.txt` to use live analysis."
            ) from exc

        client = genai.Client(api_key=key)

        def transport(messages, model, *, max_output_tokens=None):
            from rag.budget import Usage

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
            meta = getattr(response, "usage_metadata", None)
            text = response.text or ""
            if meta is None:
                return text
            return text, Usage(
                int(getattr(meta, "prompt_token_count", 0) or 0),
                int(getattr(meta, "candidates_token_count", 0) or 0),
            )

        return transport

    def _output_cap(self) -> Optional[int]:
        """The per-call output ceiling: explicit, else the budget's, else none."""
        if self._max_output_tokens is not None:
            return self._max_output_tokens
        if self._budget is not None:
            from rag.budget import SERVICE_LLM

            return self._budget.limits_for(SERVICE_LLM).max_output_tokens_per_call
        return None

    def _transport_takes_cap(self) -> bool:
        """Whether this transport accepts ``max_output_tokens``.

        Asked once, by signature rather than by calling and catching TypeError:
        a TypeError raised *inside* a transport would otherwise read as "it does
        not take the argument", and the call would be made a second time —
        double-charging the budget this exists to protect.
        """
        if self._takes_cap is None:
            try:
                parameters = inspect.signature(self._transport).parameters
            except (TypeError, ValueError):  # builtins and C callables
                self._takes_cap = False
            else:
                self._takes_cap = "max_output_tokens" in parameters or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in parameters.values()
                )
        return self._takes_cap

    def _call(self, messages: List[Dict[str, str]]) -> str:
        """One generation call: reserved, retried on quota only, then recorded.

        Metering here rather than in :meth:`analyze_page` is deliberate. This is
        the one place every request passes through — including the corrective
        JSON retry and each quota retry, all of which are real calls the free
        tier charges for.
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
                # Worst case: a reply that runs all the way to the ceiling.
                # Never start a call that could not be afforded at full length.
                estimated_output=cap or 0,
            )

        def invoke():
            try:
                result = (
                    self._transport(messages, self.model, max_output_tokens=cap)
                    if cap is not None and self._transport_takes_cap()
                    else self._transport(messages, self.model)
                )
            except Exception:
                # A rejected request is still a request: it counts against the
                # daily request limit even though it returned no tokens. Not
                # counting it is how a retry storm quietly outspends its budget.
                if self._budget is not None:
                    self._budget.record(
                        SERVICE_LLM, self.model, input_tokens=0, output_tokens=0
                    )
                raise
            if self._budget is not None:
                text, usage = result if isinstance(result, tuple) else (result, None)
                if usage is None:
                    # No usage metadata: the estimate is the only count there is.
                    usage = Usage(estimated_input, estimate_tokens(text))
                self._budget.record(
                    SERVICE_LLM, self.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
            return result

        result = call_with_quota_backoff(
            invoke,
            max_retries=self._max_retries,
            sleep=self._sleep,
            jitter=self._jitter,
            exhausted_message=(
                f"Google AI quota exhausted after {self._max_retries} retries. "
                "The free tier limits requests per minute; wait for the window "
                "to reset or re-run with --no-llm."
            ),
        )
        return result[0] if isinstance(result, tuple) else result

    # -- validated generation ---------------------------------------------- #
    def _generate_validated(self, messages: List[Dict[str, str]], model_cls):
        """Call, parse and validate, with one corrective retry."""
        attempt_messages = list(messages)
        problem: Optional[str] = None
        for attempt in range(2):
            raw = self._call(attempt_messages)
            try:
                return model_cls.model_validate(json.loads(extract_json(raw)))
            except (
                InvalidModelOutputError,
                json.JSONDecodeError,
                ValidationError,
            ) as exc:
                problem = str(exc)[:500]
                if attempt == 1:
                    break
                attempt_messages = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply was not valid JSON matching the "
                            f"required shape. The error was: {problem}\n"
                            "Reply again with only the JSON object."
                        ),
                    }
                ]
        raise InvalidModelOutputError(
            f"Model did not return valid JSON after 2 attempts: {problem}"
        )

    def analyze_page(self, prompt: GroundedPrompt) -> LlmPageAnalysis:
        """Analyse one page from its grounded prompt."""
        messages = prompt.as_messages()
        messages[-1] = {
            "role": messages[-1]["role"],
            "content": messages[-1]["content"] + JSON_INSTRUCTION,
        }
        return self._generate_validated(messages, LlmPageAnalysis)

    def summarize(self, payload: str) -> LlmSummary:
        """Write the executive summary from already-produced findings."""
        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": payload},
        ]
        return self._generate_validated(messages, LlmSummary)
