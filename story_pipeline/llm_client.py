"""role 単位の通信再試行とモデルフォールバック。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import random
import threading
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
class TransportAttempt:
    """1回の HTTP transport 試行に関する秘密値を含まない計測値。"""

    model_reference: str
    api_model: str
    attempt: int
    maximum_attempts: int
    started_at: str
    finished_at: str
    elapsed_ms: int
    result: str
    failure_kind: str | None
    wait_ms: int


@dataclass(frozen=True, slots=True)
class CompletionResult:
    response: ChatResponse
    model_reference: str
    attempts: int
    fallbacks: tuple[FallbackEvent, ...]
    transport_attempts: tuple[TransportAttempt, ...] = ()


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
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        heartbeat_interval_seconds: float = 30.0,
    ) -> None:
        self.config = config
        self.environment = environment
        self.transport = transport or ChatTransport()
        self.sleep = sleep
        self.random_factor = random_factor
        self.notify = notify or (lambda _: None)
        self.monotonic = monotonic
        self.utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.event_sink = event_sink or (lambda _: None)
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._events: list[dict[str, Any]] = []
        self._event_lock = threading.Lock()

    def drain_events(self) -> tuple[dict[str, Any], ...]:
        """現在までの構造化イベントを取り出し、内部 buffer を空にする。"""
        with self._event_lock:
            events = tuple(self._events)
            self._events.clear()
        return events

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
        transport_attempts: list[TransportAttempt] = []
        for reference in references:
            if previous is not None and last_failure is not None:
                fallbacks.append(FallbackEvent(previous, reference, last_failure.kind))
                self._emit("fallback", {
                    "role": role, "source": previous, "target": reference,
                    "failure_kind": last_failure.kind,
                })
            try:
                response, attempts, measured = self._complete_model(reference, messages, response_format)
                total_attempts += attempts
                transport_attempts.extend(measured)
                return CompletionResult(
                    response, reference, total_attempts, tuple(fallbacks), tuple(transport_attempts)
                )
            except _ModelExhausted as exhausted:
                total_attempts += exhausted.attempts
                transport_attempts.extend(exhausted.transport_attempts)
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
            response, attempts, _ = self._complete_model(
                reference, messages, None, max_tokens_override=128
            )
        except _ModelExhausted as exhausted:
            raise exhausted.failure from None
        text = response.content.strip().strip("`*\"'.# \t\n")
        if text.upper() != "OK" and "OK" not in text.upper().split():
            raise ApiFailure("invalid_response", "接続確認の固定応答が OK ではありません")
        return attempts

    def probe_structured_output(self, reference: str) -> int:
        """JSON Schema に従う最小応答を生成できることを確認する。"""
        messages = [
            {"role": "system", "content": "Return the requested JSON object only."},
            {"role": "user", "content": 'Return {"ok": true}.'},
        ]
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": "story_pipeline_capability_check",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean", "const": True}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        }
        try:
            response, attempts, _ = self._complete_model(
                reference, messages, response_format, max_tokens_override=256
            )
        except _ModelExhausted as exhausted:
            raise exhausted.failure from None
        try:
            value = json.loads(response.content)
        except (TypeError, ValueError):
            raise ApiFailure("invalid_response", "構造化応答を JSON object として解析できません") from None
        if value != {"ok": True}:
            raise ApiFailure("invalid_response", "構造化応答が要求した schema に適合しません")
        return attempts

    def _complete_model(
        self,
        reference: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None,
        *,
        max_tokens_override: int | None = None,
    ) -> tuple[ChatResponse, int, tuple[TransportAttempt, ...]]:
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
        measured: list[TransportAttempt] = []
        for attempt in range(1, maximum_attempts + 1):
            started_at = _timestamp(self.utc_now())
            started = self.monotonic()
            self._emit("transport_started", {
                "model_reference": reference,
                "api_model": model["model"],
                "attempt": attempt,
                "maximum_attempts": maximum_attempts,
            })
            stop_heartbeat = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat,
                args=(stop_heartbeat, reference, model["model"], attempt, started),
                daemon=True,
            )
            heartbeat.start()
            try:
                response = self.transport.complete(
                        base_url=provider["base_url"],
                        api_key=api_key,
                        model=model["model"],
                        messages=messages,
                        max_tokens=model["max_tokens"] if max_tokens_override is None else max_tokens_override,
                        parameters=dict(parameters),
                        timeout=self.config["request"]["timeout_seconds"],
                        response_format=response_format,
                )
                stop_heartbeat.set()
                heartbeat.join()
                finished = self.monotonic()
                measured.append(TransportAttempt(
                    reference,
                    model["model"],
                    attempt,
                    maximum_attempts,
                    started_at,
                    _timestamp(self.utc_now()),
                    _milliseconds(finished - started),
                    "completed",
                    None,
                    0,
                ))
                self._emit("transport_completed", {
                    "model_reference": reference, "api_model": model["model"],
                    "attempt": attempt, "elapsed_ms": _milliseconds(finished - started),
                })
                return response, attempt, tuple(measured)
            except ApiFailure as failure:
                stop_heartbeat.set()
                heartbeat.join()
                finished = self.monotonic()
                self._emit("transport_failed", {
                    "model_reference": reference, "api_model": model["model"],
                    "attempt": attempt, "elapsed_ms": _milliseconds(finished - started),
                    "failure_kind": failure.kind,
                })
                can_remove = (
                    failure.kind == "unsupported_parameter"
                    and not removed_parameter
                    and failure.unsupported_parameter in parameters
                    and attempt < maximum_attempts
                )
                if can_remove:
                    measured.append(TransportAttempt(
                        reference, model["model"], attempt, maximum_attempts,
                        started_at, _timestamp(self.utc_now()), _milliseconds(finished - started),
                        "failed", failure.kind, 0,
                    ))
                    removed_parameter = True
                    del parameters[failure.unsupported_parameter]
                    self.notify(
                        f"Retrying {reference}: unsupported optional parameter "
                        f"{failure.unsupported_parameter}"
                    )
                    continue
                if not failure.retryable or attempt >= maximum_attempts:
                    measured.append(TransportAttempt(
                        reference, model["model"], attempt, maximum_attempts,
                        started_at, _timestamp(self.utc_now()), _milliseconds(finished - started),
                        "failed", failure.kind, 0,
                    ))
                    raise _ModelExhausted(failure, attempt, tuple(measured)) from None
                delay = _retry_delay(failure.retry_after, attempt, self.random_factor())
                measured.append(TransportAttempt(
                    reference, model["model"], attempt, maximum_attempts,
                    started_at, _timestamp(self.utc_now()), _milliseconds(finished - started),
                    "failed", failure.kind, _milliseconds(delay),
                ))
                self.notify(f"Retrying {reference} after {delay:.1f}s: {failure.kind}")
                self._emit("retry_wait", {
                    "model_reference": reference, "attempt": attempt,
                    "failure_kind": failure.kind, "wait_ms": _milliseconds(delay),
                })
                self.sleep(delay)
            except BaseException:
                stop_heartbeat.set()
                heartbeat.join()
                raise
        raise AssertionError("通信試行ループが結果を返しませんでした")

    def _heartbeat(
        self,
        stop: threading.Event,
        reference: str,
        api_model: str,
        attempt: int,
        started: float,
    ) -> None:
        while not stop.wait(self.heartbeat_interval_seconds):
            self._emit("heartbeat", {
                "model_reference": reference,
                "api_model": api_model,
                "attempt": attempt,
                "elapsed_ms": _milliseconds(self.monotonic() - started),
            })

    def _emit(self, kind: str, details: dict[str, Any]) -> None:
        event = {
            "kind": kind,
            "occurred_at": _timestamp(self.utc_now()),
            "details": details,
        }
        with self._event_lock:
            self._events.append(event)
        self.event_sink(event)


@dataclass(frozen=True, slots=True)
class _ModelExhausted(Exception):
    failure: ApiFailure
    attempts: int
    transport_attempts: tuple[TransportAttempt, ...]


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))


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
