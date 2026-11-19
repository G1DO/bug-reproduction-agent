"""Call the model under a per-issue spend cap.

This is the only module that calls the provider. The request uses the
OpenAI-compatible HTTP API directly, as decision 0005 chose, so no SDK sits
between the tool and the provider.

A call checks its worst case before it is sent: the input bound times the
cache-miss input price plus ``max_tokens`` times the output price. The cap is
checked against that worst case, not against the previous call's actual usage.
After a call the actual usage is charged. A failed or timed-out attempt is
charged its worst case.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from email.message import Message
from enum import StrEnum
from http.client import HTTPException
from typing import IO, Final, cast

from bugrepro.prices import (
    ENFORCED_PRICING_MODE,
    PRICES,
    PriceTier,
    actual_cost_usd,
    affordable_max_tokens,
    input_bound_tokens,
    worst_case_usd,
)
from bugrepro.sandbox import Deadline

DEFAULT_BASE_URL: Final = "https://api.deepseek.com"
DEFAULT_MODEL: Final = "deepseek-flash"
DEFAULT_COST_CAP_USD: Final = Decimal("1.00")
DEFAULT_MAX_TOKENS: Final = 4096
MIN_USEFUL_MAX_TOKENS: Final = 1024
MAX_ATTEMPTS: Final = 3
PROVIDER_TIMEOUT_S: Final = 60.0
RETRY_BACKOFF_S: Final = 0.5
MAX_ERROR_BYTES: Final = 65_536
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
MAX_MODEL_CHARS: Final = 128
REDACTED: Final = "[redacted]"

KNOWN_FINISH_REASONS: Final = frozenset(
    {
        "stop",
        "length",
        "content_filter",
        "tool_calls",
        "insufficient_system_resource",
        "aborted",
    }
)

Transport = Callable[[urllib.request.Request, float], bytes]


class ModelError(Exception):
    """A model call could not be set up or completed."""


class MissingApiKey(ModelError):
    """The provider key is not set."""


class UnpricedModel(ModelError):
    """The model ID has no entry in the price table."""


class UnpricedPricingMode(ModelError):
    """The enforced pricing mode has no entry for the model."""


class CostCapExceeded(ModelError):
    """A charge would take the run over its cap."""


class Outcome(StrEnum):
    """What a model call produced."""

    OK = "ok"
    STOPPED_AT_COST_CAP = "stopped-at-cost-cap"
    ERROR = "error"


@dataclass(frozen=True)
class Usage:
    """The token usage the provider reported."""

    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int = 0


@dataclass(frozen=True)
class CallCost:
    """One charged attempt in the run log."""

    cost_usd: Decimal
    kind: str
    max_tokens: int
    usage: Usage | None = None
    model: str | None = None


@dataclass
class Budget:
    """The cap and spend for one serial issue run.

    ``calls`` holds one entry per attempt, including an attempt charged its
    worst case after a timeout. The sum of those costs is ``spent_usd``. A
    budget is not thread-safe: one run's calls must be serial.
    """

    cap_usd: Decimal
    spent_usd: Decimal = Decimal(0)
    calls: list[CallCost] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.cap_usd.is_finite() or self.cap_usd < 0:
            raise ValueError("the cost cap must be zero or greater")
        if not self.spent_usd.is_finite() or self.spent_usd < 0:
            raise ValueError("spent cost must be zero or greater")
        if self.spent_usd > self.cap_usd:
            raise ValueError("spent cost already exceeds the cap")

    @property
    def remaining_usd(self) -> Decimal:
        return self.cap_usd - self.spent_usd

    def charge(
        self,
        cost_usd: Decimal,
        *,
        kind: str,
        max_tokens: int,
        usage: Usage | None = None,
        model: str | None = None,
    ) -> None:
        if not cost_usd.is_finite() or cost_usd < 0:
            raise ValueError("cost must be zero or greater")
        if self.spent_usd + cost_usd > self.cap_usd:
            raise CostCapExceeded(
                "a charge would take the run over its cost cap; "
                "the worst-case check has a bug"
            )
        self.spent_usd += cost_usd
        self.calls.append(
            CallCost(
                cost_usd=cost_usd,
                kind=kind,
                max_tokens=max_tokens,
                usage=usage,
                model=model,
            )
        )

    def log(self) -> list[dict[str, object]]:
        return [
            {
                "cost_usd": float(call.cost_usd),
                "kind": call.kind,
                "max_tokens": call.max_tokens,
                "model": call.model,
                "input_tokens": call.usage.prompt_tokens if call.usage else None,
                "output_tokens": (call.usage.completion_tokens if call.usage else None),
            }
            for call in self.calls
        ]


@dataclass(frozen=True)
class ModelResult:
    """The outcome of one requested model call."""

    outcome: Outcome
    text: str | None = None
    usage: Usage | None = None
    model: str | None = None
    finish_reason: str | None = None
    error: str | None = None


class _ProviderError(Exception):
    """A provider reply that did not produce a usable completion."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the key cannot be forwarded to another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused", headers, fp
        )


_OPENER: Final = urllib.request.build_opener(_NoRedirect)


def _redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, REDACTED)


def _int_field(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"invalid {name}")
    if value < 0:
        raise ValueError(f"invalid {name}")
    return value


class ModelClient:
    """One issue's model calls, bound to one budget.

    Calls share one budget and must be serial. The client is not thread-safe.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        budget: Budget,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = PROVIDER_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise MissingApiKey("BUGREPRO_API_KEY is not set")
        if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in api_key):
            raise ModelError("BUGREPRO_API_KEY contains a control character")
        try:
            api_key.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ModelError(
                "BUGREPRO_API_KEY contains a character that cannot be sent "
                "in an HTTP header"
            ) from exc
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ModelError("BUGREPRO_BASE_URL must be an http or https URL")
        if parsed.username is not None or parsed.password is not None:
            raise ModelError("BUGREPRO_BASE_URL must not contain credentials")
        model_prices = PRICES.get(model)
        if model_prices is None:
            raise UnpricedModel(
                f"no price entry for model {model!r}; add one in src/bugrepro/prices.py"
            )
        tier = model_prices.tiers.get(ENFORCED_PRICING_MODE)
        if tier is None:
            raise UnpricedPricingMode(
                f"no price entry for model {model!r} in pricing mode "
                f"{ENFORCED_PRICING_MODE!r}"
            )
        self._model = model
        self._api_key = api_key
        self._budget = budget
        self._tier: PriceTier = tier
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._transport = transport or self._post
        self._sleep = sleep
        self._timeout_s = timeout_s

    @classmethod
    def from_environment(cls, *, budget: Budget) -> ModelClient:
        api_key = os.environ.get("BUGREPRO_API_KEY", "")
        if not api_key:
            raise MissingApiKey("BUGREPRO_API_KEY is not set")
        model = os.environ.get("BUGREPRO_MODEL") or DEFAULT_MODEL
        base_url = os.environ.get("BUGREPRO_BASE_URL") or DEFAULT_BASE_URL
        return cls(model=model, api_key=api_key, budget=budget, base_url=base_url)

    @property
    def model(self) -> str:
        return self._model

    @property
    def budget(self) -> Budget:
        """The budget this client charges; the caller can share it."""
        return self._budget

    @property
    def log_model(self) -> str:
        """The configured model ID, safe to put in the run log."""
        return _redact(self._model, self._api_key)[:MAX_MODEL_CHARS]

    def complete(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        max_tokens: int,
        deadline: Deadline,
    ) -> ModelResult:
        if max_tokens < MIN_USEFUL_MAX_TOKENS:
            raise ValueError(f"max_tokens must be at least {MIN_USEFUL_MAX_TOKENS}")
        last_error: str | None = None
        for attempt in range(MAX_ATTEMPTS):
            fitted = self._fit(messages, max_tokens)
            if fitted is None:
                return ModelResult(Outcome.STOPPED_AT_COST_CAP, error=last_error)
            chosen, body, worst = fitted
            timeout = deadline.bounded(self._timeout_s)
            try:
                request = self._request(body)
                raw = self._transport(request, timeout)
                text, usage, response_model, finish_reason = self._parse(
                    raw, chosen, len(body)
                )
            except TimeoutError:
                self._charge_worst(worst, chosen)
                last_error = "the model call timed out"
            except _ProviderError as exc:
                self._charge_worst(worst, chosen)
                last_error = exc.message
                if not exc.retryable:
                    return ModelResult(Outcome.ERROR, error=last_error)
            except (TypeError, ValueError) as exc:
                self._charge_worst(worst, chosen)
                last_error = _redact(
                    f"the provider returned an invalid response: {exc}",
                    self._api_key,
                )
            else:
                cost = actual_cost_usd(
                    self._tier,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.cache_hit_tokens,
                )
                if cost > worst:
                    self._charge_worst(worst, chosen)
                    return ModelResult(
                        Outcome.ERROR,
                        error=(
                            "the provider reported usage above the quoted worst case"
                        ),
                    )
                self._budget.charge(
                    cost,
                    kind="actual",
                    max_tokens=chosen,
                    usage=usage,
                    model=response_model,
                )
                if finish_reason == "length" and not (
                    text.strip().replace(REDACTED, "").strip()
                ):
                    return ModelResult(
                        Outcome.ERROR,
                        usage=usage,
                        model=response_model,
                        finish_reason=finish_reason,
                        error=(
                            "the model used the whole output budget before "
                            "producing an answer"
                        ),
                    )
                return ModelResult(
                    Outcome.OK,
                    text=text,
                    usage=usage,
                    model=response_model,
                    finish_reason=finish_reason,
                )
            if attempt + 1 < MAX_ATTEMPTS:
                delay = min(RETRY_BACKOFF_S * (2**attempt), deadline.remaining())
                if delay > 0:
                    self._sleep(delay)
        return ModelResult(Outcome.ERROR, error=last_error or "the model call failed")

    def _fit(
        self,
        messages: Sequence[Mapping[str, object]],
        requested: int,
    ) -> tuple[int, bytes, Decimal] | None:
        body = self._body(messages, requested)
        remaining = self._budget.remaining_usd
        input_only = worst_case_usd(self._tier, len(body), 0)
        if input_only >= remaining:
            return None
        affordable = affordable_max_tokens(self._tier, len(body), remaining)
        chosen = min(requested, affordable)
        if chosen < MIN_USEFUL_MAX_TOKENS:
            return None
        body = self._body(messages, chosen)
        worst = worst_case_usd(self._tier, len(body), chosen)
        if worst > remaining:
            return None
        return chosen, body, worst

    def _body(self, messages: Sequence[Mapping[str, object]], max_tokens: int) -> bytes:
        payload = {
            "model": self._model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "stream": False,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )

    def _request(self, body: bytes) -> urllib.request.Request:
        return urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )

    def _charge_worst(self, worst: Decimal, max_tokens: int) -> None:
        self._budget.charge(worst, kind="worst-case", max_tokens=max_tokens)

    def _post(self, request: urllib.request.Request, timeout: float) -> bytes:
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                body = cast(bytes, response.read(MAX_RESPONSE_BYTES + 1))
            if len(body) > MAX_RESPONSE_BYTES:
                raise _ProviderError(
                    "the provider response was too large", retryable=False
                )
            return body
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(MAX_ERROR_BYTES)
            except OSError:
                body = b""
            raise _ProviderError(
                self._http_error_message(exc, body),
                retryable=exc.code >= 500 or exc.code == 429,
            ) from exc
        except urllib.error.URLError as exc:
            message = _redact(
                f"the provider could not be reached: {exc.reason}",
                self._api_key,
            )
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(message) from exc
            raise _ProviderError(message, retryable=True) from exc
        except TimeoutError:
            raise
        except OSError as exc:
            raise _ProviderError(
                _redact(f"the provider could not be reached: {exc}", self._api_key),
                retryable=True,
            ) from exc
        except HTTPException as exc:
            raise _ProviderError(
                _redact(f"the provider could not be reached: {exc}", self._api_key),
                retryable=True,
            ) from exc

    def _http_error_message(self, exc: urllib.error.HTTPError, body: bytes) -> str:
        message: str | None = None
        try:
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                raw = error.get("message")
                if isinstance(raw, str):
                    message = raw
            elif isinstance(error, str):
                message = error
            if message is None:
                raw = parsed.get("message")
                if isinstance(raw, str):
                    message = raw
        if message is None:
            text = body.decode("utf-8", "replace").strip()
            message = text or f"HTTP {exc.code} {exc.reason}"
        message = _redact(message, self._api_key)
        return f"the provider returned HTTP {exc.code}: {message[:500]}"

    def _parse(
        self, raw: bytes, max_tokens: int, request_bytes: int
    ) -> tuple[str, Usage, str | None, str | None]:
        parsed: object = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError("response is not an object")
        data = cast(dict[str, object], parsed)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("response has no choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise TypeError("response choice is not an object")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise TypeError("response message is not an object")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise TypeError("response content is not text")
        finish = choice.get("finish_reason")
        finish_reason = None
        if isinstance(finish, str):
            candidate = _redact(finish, self._api_key).strip().casefold()
            if candidate in KNOWN_FINISH_REASONS:
                finish_reason = candidate
        usage_raw = data.get("usage")
        if not isinstance(usage_raw, dict):
            raise TypeError("response has no usage")
        prompt = _int_field(usage_raw.get("prompt_tokens"), "prompt_tokens")
        completion = _int_field(usage_raw.get("completion_tokens"), "completion_tokens")
        cache_hit = _int_field(
            usage_raw.get("prompt_cache_hit_tokens", 0),
            "prompt_cache_hit_tokens",
        )
        if prompt > input_bound_tokens(request_bytes):
            raise ValueError("provider prompt tokens exceed the input bound")
        if completion > max_tokens:
            raise ValueError("provider completion tokens exceed max_tokens")
        if cache_hit > prompt:
            raise ValueError("provider cache-hit tokens exceed prompt tokens")
        response_model = data.get("model")
        model = (
            _redact(response_model, self._api_key)[:MAX_MODEL_CHARS]
            if isinstance(response_model, str)
            else None
        )
        return (
            _redact(content or "", self._api_key),
            Usage(prompt, completion, cache_hit),
            model,
            finish_reason,
        )
