"""OpenAI 互換 Chat Completions の低水準 HTTP transport。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import socket
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from story_pipeline.secrets import SecretSanitizer


ErrorKind = Literal[
    "authentication",
    "invalid_request",
    "unsupported_parameter",
    "model_unavailable",
    "rate_limit",
    "temporary",
    "context_length",
    "output_truncated",
    "safety_refusal",
    "invalid_response",
]


@dataclass(frozen=True, slots=True)
class ChatResponse:
    content: str
    model: str
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class ApiFailure(Exception):
    kind: ErrorKind
    message: str
    status: int | None = None
    retry_after: str | None = None
    unsupported_parameter: str | None = None

    @property
    def retryable(self) -> bool:
        return self.kind in {"rate_limit", "temporary"}

    @property
    def fallback_allowed(self) -> bool:
        return self.kind in {
            "model_unavailable", "rate_limit", "temporary", "output_truncated", "invalid_response"
        }

    @property
    def awaiting_human(self) -> bool:
        return self.kind in {"context_length", "safety_refusal"}

    def __str__(self) -> str:
        return self.message


OpenUrl = Callable[..., Any]


class ChatTransport:
    """単一モデルへの1回の非ストリーミング呼び出しを行う。"""

    def __init__(self, *, open_url: OpenUrl = urlopen) -> None:
        self._open_url = open_url

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        parameters: dict[str, Any],
        timeout: int,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            **parameters,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        request = Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        sanitizer = SecretSanitizer([api_key])
        try:
            with self._open_url(request, timeout=timeout) as response:
                body = response.read(1_048_577)
        except HTTPError as error:
            body = error.read(65_537)
            message = _error_message(body, sanitizer)
            raise _classify_http_error(
                error.code,
                message,
                error.headers.get("Retry-After"),
                set(parameters),
            ) from None
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            message = sanitizer.sanitize(str(getattr(error, "reason", error)), limit=300)
            raise ApiFailure("temporary", message or "通信に失敗しました") from None
        if len(body) > 1_048_576:
            raise ApiFailure("invalid_response", "API 応答が許容サイズを超えています")
        return _parse_success(body, sanitizer)


def _parse_success(body: bytes, sanitizer: SecretSanitizer) -> ChatResponse:
    try:
        value = json.loads(body.decode("utf-8"))
        choice = value["choices"][0]
        message = choice["message"]
        refusal = message.get("refusal")
        if refusal:
            raise ApiFailure("safety_refusal", "モデルが安全性上の理由で応答を拒否しました")
        content = message["content"]
        model = value.get("model", "")
        finish_reason = choice.get("finish_reason")
        if not isinstance(content, str) or not isinstance(model, str):
            raise (TypeError)
        if finish_reason == "length":
            raise ApiFailure("output_truncated", "モデル出力がトークン上限で切断されました")
        return ChatResponse(content, model, finish_reason if isinstance(finish_reason, str) else None)
    except ApiFailure:
        raise
    except (UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        message = _error_message(body, sanitizer)
        raise ApiFailure("invalid_response", message or "API 応答形式が不正です") from None


def _error_message(body: bytes, sanitizer: SecretSanitizer) -> str:
    try:
        value = json.loads(body[:65_536].decode("utf-8", errors="replace"))
        error = value.get("error", {}) if isinstance(value, dict) else {}
        raw = error.get("message", "") if isinstance(error, dict) else ""
        if not isinstance(raw, str):
            raw = ""
    except json.JSONDecodeError:
        raw = ""
    return sanitizer.sanitize(raw, limit=300)


def _classify_http_error(
    status: int,
    message: str,
    retry_after: str | None,
    optional_parameters: set[str],
) -> ApiFailure:
    lowered = message.lower()
    if status in {401, 403}:
        return ApiFailure("authentication", message or "API の認証または権限を確認できません", status)
    if "context" in lowered and any(word in lowered for word in {"length", "window", "token", "maximum"}):
        return ApiFailure("context_length", message or "コンテキスト長を超えました", status)
    if any(word in lowered for word in {"safety", "policy", "moderation", "refusal"}):
        return ApiFailure("safety_refusal", "モデルが安全性上の理由で応答を拒否しました", status)
    if status in {400, 422}:
        parameter = _mentioned_parameter(message, optional_parameters)
        if parameter is not None:
            return ApiFailure("unsupported_parameter", message, status, unsupported_parameter=parameter)
        return ApiFailure("invalid_request", message or "API リクエストが不正です", status)
    if status == 404:
        return ApiFailure("model_unavailable", message or "モデルまたは endpoint が見つかりません", status)
    if status == 429:
        return ApiFailure("rate_limit", message or "API のレート制限に達しました", status, retry_after)
    if status in {500, 502, 503, 504}:
        return ApiFailure("temporary", message or "API で一時障害が発生しました", status, retry_after)
    return ApiFailure("invalid_response", message or f"API が HTTP {status} を返しました", status)


def _mentioned_parameter(message: str, candidates: set[str]) -> str | None:
    lowered = message.lower()
    for parameter in sorted(candidates):
        escaped = parameter.lower()
        if (
            f"'{escaped}'" in lowered
            or f'"{escaped}"' in lowered
            or f"parameter {escaped}" in lowered
            or f"field {escaped}" in lowered
        ):
            return parameter
    return None
