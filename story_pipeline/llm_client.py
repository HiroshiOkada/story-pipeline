"""role 単位の通信再試行とモデルフォールバック。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time
from typing import Any

from story_pipeline.environment import require_api_key
from story_pipeline.llm_transport import ApiFailure, ChatResponse, ChatTransport


@dataclass(frozen=True, slots=True)
class FallbackEvent:
    source: str
    target: str
    reason: str


@dataclass(frozen=True, slots=True)
class CompletionResult:
    response: ChatResponse
    model_reference: str
    attempts: int
    fallbacks: tuple[FallbackEvent, ...]


class LLMClient:
    """設定された role のモデルを、意味を変えず順番に試す。"""

    def __init__(
        self,
        config: dict[str, Any],
        environment: Mapping[str, str],
        *,
        transport: ChatTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_factor: Callable[[], float] = random.random,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.environment = environment
        self.transport = transport or ChatTransport()
        self.sleep = sleep
        self.random_factor = random_factor
        self.notify = notify or (lambda _: None)

    def complete_role(
        self,
        role: str,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
    ) -> CompletionResult:
        references = self.config["roles"][role]
        total_attempts = 0
        fallbacks: list[FallbackEvent] = []
        previous: str | None = None
        last_failure: ApiFailure | None = None
        for reference in references:
            if previous is not None and last_failure is not None:
                fallbacks.append(FallbackEvent(previous, reference, last_failure.kind))
            try:
                response, attempts = self._complete_model(reference, messages, response_format)
                total_attempts += attempts
                return CompletionResult(response, reference, total_attempts, tuple(fallbacks))
            except _ModelExhausted as exhausted:
                total_attempts += exhausted.attempts
                last_failure = exhausted.failure
                previous = reference
                if not exhausted.failure.fallback_allowed:
                    raise exhausted.failure
        assert last_failure is not None
        raise last_failure

    def probe_model(self, reference: str) -> int:
        """創作 count 外の固定 `OK` 応答で単一モデルの接続を確認する。"""
        messages = [
            {"role": "system", "content": "Reply with exactly OK."},
            {"role": "user", "content": "Connection check. Reply with exactly OK."},
        ]
        try:
            response, attempts = self._complete_model(reference, messages, None, max_tokens_override=8)
        except _ModelExhausted as exhausted:
            raise exhausted.failure from None
        if response.content.strip() != "OK":
            raise ApiFailure("invalid_response", "接続確認の固定応答が OK ではありません")
        return attempts

    def _complete_model(
        self,
        reference: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None,
        *,
        max_tokens_override: int | None = None,
    ) -> tuple[ChatResponse, int]:
        model = self.config["models"][reference]
        provider_name = model["provider"]
        provider = self.config["providers"][provider_name]
        api_key = require_api_key(provider_name, provider, self.environment)
        parameters = dict(model["parameters"])
        maximum_attempts = min(
            self.config["request"]["retry_attempts"],
            self.config["limits"]["retry_calls_per_request"],
        )
        removed_parameter = False
        for attempt in range(1, maximum_attempts + 1):
            try:
                return (
                    self.transport.complete(
                        base_url=provider["base_url"],
                        api_key=api_key,
                        model=model["model"],
                        messages=messages,
                        max_tokens=model["max_tokens"] if max_tokens_override is None else max_tokens_override,
                        parameters=dict(parameters),
                        timeout=self.config["request"]["timeout_seconds"],
                        response_format=response_format,
                    ),
                    attempt,
                )
            except ApiFailure as failure:
                can_remove = (
                    failure.kind == "unsupported_parameter"
                    and not removed_parameter
                    and failure.unsupported_parameter in parameters
                    and attempt < maximum_attempts
                )
                if can_remove:
                    removed_parameter = True
                    del parameters[failure.unsupported_parameter]
                    self.notify(
                        f"Retrying {reference}: unsupported optional parameter "
                        f"{failure.unsupported_parameter}"
                    )
                    continue
                if not failure.retryable or attempt >= maximum_attempts:
                    raise _ModelExhausted(failure, attempt) from None
                delay = _retry_delay(failure.retry_after, attempt, self.random_factor())
                self.notify(f"Retrying {reference} after {delay:.1f}s: {failure.kind}")
                self.sleep(delay)
        raise AssertionError("通信試行ループが結果を返しませんでした")


@dataclass(frozen=True, slots=True)
class _ModelExhausted(Exception):
    failure: ApiFailure
    attempts: int


def _retry_delay(retry_after: str | None, attempt: int, random_value: float) -> float:
    if retry_after:
        try:
            seconds = float(retry_after)
            if 0 <= seconds <= 300:
                return seconds
        except ValueError:
            try:
                target = parsedate_to_datetime(retry_after)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                seconds = (target - datetime.now(timezone.utc)).total_seconds()
                if 0 <= seconds <= 300:
                    return seconds
            except (TypeError, ValueError, OverflowError):
                pass
    base = min(60.0, float(2 ** (attempt - 1)))
    jitter = 0.5 + max(0.0, min(1.0, random_value))
    return base * jitter
