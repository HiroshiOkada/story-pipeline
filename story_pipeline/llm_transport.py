"""OpenAI 互換 Chat Completions の低水準 HTTP transport。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import math
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
class TokenUsage:
    """provider が返した token 利用量。欠落値は推測しない。"""

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class ChatResponse:
    content: str
    model: str
    finish_reason: str | None
    usage: TokenUsage | None = None


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


# kind ごとの利用者向け案内。(概要, 復旧案) の順。
_FAILURE_GUIDANCE: dict[ErrorKind, tuple[str, str]] = {
    "authentication": (
        "API の認証に失敗しました",
        "設定の api_key_env が示す環境変数に API キーが正しく設定されているか確認してください",
    ),
    "invalid_request": (
        "API が要求を受け付けられませんでした",
        "設定のモデル識別子と parameters を確認してください",
    ),
    "unsupported_parameter": (
        "モデルが受け付けない設定項目があります",
        "設定の parameters から該当項目を取り除いてください",
    ),
    "model_unavailable": (
        "モデルが現在利用できません",
        "時間をおいて再実行するか、設定のモデル識別子を見直してください",
    ),
    "rate_limit": (
        "API の利用制限に達しました",
        "時間をおいて再実行してください。繰り返す場合は provider の利用上限を確認してください",
    ),
    "temporary": (
        "API への接続に失敗しました",
        "ネットワーク接続と provider の稼働状況を確認し、時間をおいて再実行してください",
    ),
    "context_length": (
        "モデルのコンテキスト長を超えました",
        "要求や追加資料を減らすか、コンテキスト長の大きいモデルへ変更してください",
    ),
    "output_truncated": (
        "モデルの出力が途中で切れました",
        "設定の max_tokens を増やすか、制作対象を分割してください",
    ),
    "safety_refusal": (
        "モデルが安全上の理由で応答を拒否しました",
        "要求や追加資料の内容を見直してください",
    ),
    "invalid_response": (
        "モデルの応答形式が不正でした",
        "時間をおいて再実行してください。繰り返す場合は設定のモデルを見直してください",
    ),
}


def describe_failure(error: ApiFailure) -> tuple[str, str]:
    """API 失敗を利用者向けの日本語の概要と復旧案へ変換する。"""
    summary, action = _FAILURE_GUIDANCE[error.kind]
    detail = error.message.strip()
    if detail:
        summary = f"{summary}(詳細: {detail})"
    return summary, action


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
        return ChatResponse(
            content,
            model,
            finish_reason if isinstance(finish_reason, str) else None,
            _normalize_usage(value.get("usage")),
        )
    except ApiFailure:
        raise
    except (UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        message = _error_message(body, sanitizer)
        raise ApiFailure("invalid_response", message or "API 応答形式が不正です") from None


def _normalize_usage(value: Any) -> TokenUsage | None:
    """OpenAI 互換 usage を正規化し、不明値は null のまま保つ。"""
    if not isinstance(value, dict):
        return None
    prompt_details = value.get("prompt_tokens_details")
    completion_details = value.get("completion_tokens_details")
    cached = value.get("cached_tokens")
    if cached is None and isinstance(prompt_details, dict):
        cached = prompt_details.get("cached_tokens")
    reasoning = value.get("reasoning_tokens")
    if reasoning is None and isinstance(completion_details, dict):
        reasoning = completion_details.get("reasoning_tokens")
    return TokenUsage(
        _usage_integer(value.get("prompt_tokens")),
        _usage_integer(value.get("completion_tokens")),
        _usage_integer(value.get("total_tokens")),
        _usage_integer(cached),
        _usage_integer(reasoning),
        _usage_number(value.get("cost")),
    )


def _usage_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= 0 else None


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
