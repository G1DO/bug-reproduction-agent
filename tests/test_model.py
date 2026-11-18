from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from typing import Self

import pytest

from bugrepro import model as model_module
from bugrepro import prices
from bugrepro.model import (
    MAX_ATTEMPTS,
    MAX_RESPONSE_BYTES,
    MIN_USEFUL_MAX_TOKENS,
    RETRY_BACKOFF_S,
    Budget,
    MissingApiKey,
    ModelClient,
    ModelError,
    Outcome,
    UnpricedModel,
)
from bugrepro.sandbox import Deadline, TimeLimitExceeded

MESSAGES = [{"role": "user", "content": "hello"}]


class _Script:
    """A model transport that returns or raises one scripted reply per call."""

    def __init__(self, replies: list[bytes | BaseException]) -> None:
        self._replies = replies
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self._replies:
            raise AssertionError("unexpected model call")
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _response(
    *,
    prompt: int = 10,
    completion: int = 5,
    cache_hit: int = 0,
    text: str = "ok",
    finish_reason: str = "stop",
) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "DeepSeek-V4.1-Flash-1017",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_cache_hit_tokens": cache_hit,
                "prompt_cache_miss_tokens": prompt - cache_hit,
            },
        }
    ).encode()


def _tier() -> prices.PriceTier:
    return prices.PRICES["deepseek-flash"].tiers[prices.ENFORCED_PRICING_MODE]


def _client(
    script: _Script,
    *,
    budget: Budget,
    model: str = "deepseek-flash",
    sleep: Callable[[float], None] | None = None,
) -> ModelClient:
    return ModelClient(
        model=model,
        api_key="test-key-not-real",
        budget=budget,
        transport=script,
        sleep=sleep or (lambda _: None),
    )


def test_stops_before_a_call_that_would_exceed_the_cap() -> None:
    script = _Script(
        [
            _response(prompt=10, completion=20_000),
            _response(prompt=10, completion=20_600),
            _response(prompt=10, completion=200_000),
        ]
    )
    budget = Budget(cap_usd=Decimal("0.05"))
    client = _client(script, budget=budget)

    first = client.complete(MESSAGES, max_tokens=21_000, deadline=Deadline())
    second = client.complete(MESSAGES, max_tokens=21_000, deadline=Deadline())
    third = client.complete(MESSAGES, max_tokens=21_000, deadline=Deadline())

    assert first.outcome is Outcome.OK
    assert second.outcome is Outcome.OK
    assert third.outcome is Outcome.STOPPED_AT_COST_CAP
    assert len(script.requests) == 2
    assert budget.spent_usd <= Decimal("0.05")
    assert sum(call.cost_usd for call in budget.calls) == budget.spent_usd
    third_cost = prices.actual_cost_usd(_tier(), 10, 200_000, 0)
    assert third_cost > budget.cap_usd


def test_refuses_a_call_whose_worst_case_exceeds_the_cap() -> None:
    script = _Script([_response(prompt=1, completion=1)])
    budget = Budget(cap_usd=Decimal("0.05"))
    client = _client(script, budget=budget)

    result = client.complete(
        [{"role": "user", "content": "x" * 200_000}],
        max_tokens=MIN_USEFUL_MAX_TOKENS,
        deadline=Deadline(),
    )

    assert result.outcome is Outcome.STOPPED_AT_COST_CAP
    assert script.requests == []
    assert budget.spent_usd == Decimal(0)
    assert budget.calls == []


def test_timed_out_attempt_is_charged_its_worst_case() -> None:
    script = _Script([TimeoutError("slow"), _response(prompt=10, completion=10)])
    budget = Budget(cap_usd=Decimal("0.05"))
    client = _client(script, budget=budget)

    result = client.complete(
        MESSAGES, max_tokens=MIN_USEFUL_MAX_TOKENS, deadline=Deadline()
    )

    assert result.outcome is Outcome.OK
    assert result.model == "DeepSeek-V4.1-Flash-1017"
    assert len(script.requests) == 2
    body = script.requests[0].data
    assert isinstance(body, bytes)
    expected = prices.worst_case_usd(_tier(), len(body), MIN_USEFUL_MAX_TOKENS)
    assert budget.calls[0].cost_usd == expected
    assert budget.calls[0].kind == "worst-case"
    assert budget.calls[0].usage is None
    assert budget.calls[1].kind == "actual"
    assert budget.spent_usd <= budget.cap_usd
    assert sum(call.cost_usd for call in budget.calls) == budget.spent_usd


def test_timeout_stops_after_three_charged_attempts() -> None:
    script = _Script([TimeoutError("slow")] * MAX_ATTEMPTS)
    budget = Budget(cap_usd=Decimal("0.05"))
    sleeps: list[float] = []
    client = _client(script, budget=budget, sleep=sleeps.append)

    result = client.complete(
        MESSAGES, max_tokens=MIN_USEFUL_MAX_TOKENS, deadline=Deadline()
    )

    assert result.outcome is Outcome.ERROR
    assert "timed out" in (result.error or "")
    assert len(script.requests) == MAX_ATTEMPTS
    assert len(budget.calls) == MAX_ATTEMPTS
    assert all(call.kind == "worst-case" for call in budget.calls)
    assert budget.spent_usd <= budget.cap_usd
    assert sum(call.cost_usd for call in budget.calls) == budget.spent_usd
    assert sleeps == [RETRY_BACKOFF_S, RETRY_BACKOFF_S * 2]


def test_unpriced_model_is_refused_before_any_call() -> None:
    script = _Script([])
    with pytest.raises(UnpricedModel):
        _client(
            script,
            budget=Budget(cap_usd=Decimal("1.00")),
            model="deepseek-unpriced",
        )
    assert script.requests == []


def test_missing_api_key_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BUGREPRO_API_KEY", raising=False)
    with pytest.raises(MissingApiKey):
        ModelClient.from_environment(budget=Budget(cap_usd=Decimal("1.00")))


def test_expired_deadline_makes_no_call() -> None:
    script = _Script([_response()])
    budget = Budget(cap_usd=Decimal("0.05"))
    client = _client(script, budget=budget)

    with pytest.raises(TimeLimitExceeded):
        client.complete(
            MESSAGES, max_tokens=MIN_USEFUL_MAX_TOKENS, deadline=Deadline(limit_s=0)
        )

    assert script.requests == []
    assert budget.spent_usd == Decimal(0)


class _ExpiringDeadline(Deadline):
    """A deadline that expires after one retry delay."""

    def __init__(self) -> None:
        super().__init__(limit_s=60)
        self.bounded_calls = 0

    def remaining(self) -> float:
        return 0.2

    def bounded(self, timeout: float | None = None) -> float:
        self.bounded_calls += 1
        if self.bounded_calls > 1:
            raise TimeLimitExceeded("the run's time limit has been reached")
        return min(timeout if timeout is not None else 0.2, 0.2)


def test_retry_sleep_does_not_pass_the_deadline() -> None:
    script = _Script([TimeoutError("slow")])
    budget = Budget(cap_usd=Decimal("0.05"))
    sleeps: list[float] = []
    client = _client(script, budget=budget, sleep=sleeps.append)

    with pytest.raises(TimeLimitExceeded):
        client.complete(
            MESSAGES,
            max_tokens=MIN_USEFUL_MAX_TOKENS,
            deadline=_ExpiringDeadline(),
        )

    assert sleeps == [0.2]
    assert len(script.requests) == 1


def test_cache_hit_usage_is_charged_at_the_cache_hit_price() -> None:
    script = _Script([_response(prompt=1000, completion=100, cache_hit=900)])
    budget = Budget(cap_usd=Decimal("0.05"))
    client = _client(script, budget=budget)

    result = client.complete(
        MESSAGES, max_tokens=MIN_USEFUL_MAX_TOKENS, deadline=Deadline()
    )

    assert result.outcome is Outcome.OK
    expected = prices.actual_cost_usd(_tier(), 1000, 100, 900)
    assert budget.calls[0].cost_usd == expected
    assert expected < prices.actual_cost_usd(_tier(), 1000, 100, 0)


def test_truncated_empty_reply_is_an_error() -> None:
    script = _Script(
        [
            _response(
                completion=MIN_USEFUL_MAX_TOKENS,
                text="",
                finish_reason="length",
            )
        ]
    )
    budget = Budget(cap_usd=Decimal("0.05"))
    client = _client(script, budget=budget)

    result = client.complete(
        MESSAGES, max_tokens=MIN_USEFUL_MAX_TOKENS, deadline=Deadline()
    )

    assert result.outcome is Outcome.ERROR
    assert result.finish_reason == "length"
    assert "output budget" in (result.error or "")
    assert budget.calls[0].kind == "actual"


def test_truncated_reply_with_text_is_reported() -> None:
    script = _Script(
        [
            _response(
                completion=MIN_USEFUL_MAX_TOKENS,
                text="partial",
                finish_reason="length",
            )
        ]
    )
    budget = Budget(cap_usd=Decimal("0.05"))
    client = _client(script, budget=budget)

    result = client.complete(
        MESSAGES, max_tokens=MIN_USEFUL_MAX_TOKENS, deadline=Deadline()
    )

    assert result.outcome is Outcome.OK
    assert result.finish_reason == "length"
    assert result.text == "partial"


class _OversizedResponse:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return b"x" * (MAX_RESPONSE_BYTES + 1)


class _OversizedOpener:
    def open(
        self, request: urllib.request.Request, timeout: float
    ) -> _OversizedResponse:
        return _OversizedResponse()


def test_oversized_provider_response_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_module, "_OPENER", _OversizedOpener())
    budget = Budget(cap_usd=Decimal("0.05"))
    client = ModelClient(
        model="deepseek-flash",
        api_key="test-key-not-real",
        budget=budget,
        sleep=lambda _: None,
    )

    result = client.complete(
        MESSAGES, max_tokens=MIN_USEFUL_MAX_TOKENS, deadline=Deadline()
    )

    assert result.outcome is Outcome.ERROR
    assert "too large" in (result.error or "")
    assert len(budget.calls) == 1
    assert budget.spent_usd <= budget.cap_usd


def test_transport_error_containing_the_key_is_redacted() -> None:
    key = "test-key-not-real"
    script = _Script(
        [ValueError(f"Invalid header value b'Bearer {key}'")] * MAX_ATTEMPTS
    )
    budget = Budget(cap_usd=Decimal("0.05"))
    client = ModelClient(
        model="deepseek-flash",
        api_key=key,
        budget=budget,
        transport=script,
        sleep=lambda _: None,
    )

    result = client.complete(
        MESSAGES, max_tokens=MIN_USEFUL_MAX_TOKENS, deadline=Deadline()
    )

    assert result.outcome is Outcome.ERROR
    assert key not in (result.error or "")
    assert "[redacted]" in (result.error or "")


def test_api_key_with_a_control_character_is_refused() -> None:
    with pytest.raises(ModelError):
        ModelClient(
            model="deepseek-flash",
            api_key="test\nkey",
            budget=Budget(cap_usd=Decimal("1.00")),
        )
