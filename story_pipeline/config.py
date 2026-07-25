"""Story Pipeline 設定の検証と正規化。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Never
from urllib.parse import urlsplit

from story_pipeline.errors import StoryPipelineError
from story_pipeline.jsonc import load_jsonc


EXIT_CONFIG = 4
REQUIRED_ROLES = {"planner", "writer", "reviewer", "reviser", "summarizer"}
LIMIT_KEYS = {
    "generation_calls",
    "review_calls",
    "revision_calls",
    "summary_calls",
    "retry_calls_per_request",
    "max_changed_lines",
}
FORBIDDEN_PARAMETERS = {
    "model", "messages", "max_tokens", "api_key", "api_key_env",
    "authorization", "base_url", "url",
}
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_config(root: Path) -> dict[str, Any]:
    """作品ルートの設定を読み、検証済みの値を返す。"""
    path = root / "story-pipeline-config.jsonc"
    value = load_jsonc(path)
    if not isinstance(value, dict):
        _invalid("トップレベルは object である必要があります。", "/")
    config = deepcopy(value)
    _keys(config, {"config_version", "dotenv", "providers", "models", "roles", "limits", "request"}, "/")
    if _integer(config["config_version"], "/config_version") != 1:
        _invalid("1 を指定してください。", "/config_version")
    _validate_dotenv(config["dotenv"], root)
    _validate_providers(config["providers"])
    _validate_models(config["models"], config["providers"])
    _validate_roles(config["roles"], config["models"])
    _validate_limits(config["limits"])
    _validate_request(config["request"])
    return config


def _validate_dotenv(value: Any, root: Path) -> None:
    obj = _object(value, "/dotenv")
    _keys(obj, {"files"}, "/dotenv")
    files = _list(obj["files"], "/dotenv/files")
    normalized: list[str] = []
    for index, item in enumerate(files):
        raw = _string(item, f"/dotenv/files/{index}")
        path = Path(raw).expanduser()
        normalized.append(str(path if path.is_absolute() else root / path))
    obj["files"] = normalized


def _validate_providers(value: Any) -> None:
    providers = _object(value, "/providers")
    if not providers:
        _invalid("1 件以上必要です。", "/providers")
    for name, provider_value in providers.items():
        location = f"/providers/{_pointer(name)}"
        provider = _object(provider_value, location)
        _keys(provider, {"base_url", "api_key_env"}, location)
        base_url = _string(provider["base_url"], f"{location}/base_url")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            _invalid("http または https の絶対 URL が必要です。", f"{location}/base_url")
        provider["base_url"] = base_url.rstrip("/")
        environment = _string(provider["api_key_env"], f"{location}/api_key_env")
        if not ENVIRONMENT_NAME.fullmatch(environment):
            _invalid("有効な環境変数名が必要です。", f"{location}/api_key_env")


def _validate_models(value: Any, providers_value: Any) -> None:
    models = _object(value, "/models")
    providers = _object(providers_value, "/providers")
    if not models:
        _invalid("1 件以上必要です。", "/models")
    for name, model_value in models.items():
        location = f"/models/{_pointer(name)}"
        model = _object(model_value, location)
        _keys(model, {"provider", "model"}, location, optional={"max_tokens", "parameters"})
        provider = _string(model["provider"], f"{location}/provider")
        if provider not in providers:
            _invalid("存在する provider を参照してください。", f"{location}/provider")
        if not _string(model["model"], f"{location}/model").strip():
            _invalid("空でないモデル識別子が必要です。", f"{location}/model")
        model["max_tokens"] = _positive_integer(model.get("max_tokens", 131072), f"{location}/max_tokens")
        parameters = _object(model.get("parameters", {}), f"{location}/parameters")
        for parameter in parameters:
            if parameter.lower() in FORBIDDEN_PARAMETERS:
                _invalid("このパラメーターは設定できません。", f"{location}/parameters/{_pointer(parameter)}")
        model["parameters"] = parameters


def _validate_roles(value: Any, models_value: Any) -> None:
    roles = _object(value, "/roles")
    _keys(roles, REQUIRED_ROLES, "/roles")
    models = _object(models_value, "/models")
    for role, references_value in roles.items():
        location = f"/roles/{role}"
        references = _list(references_value, location)
        if not references:
            _invalid("モデル参照が 1 件以上必要です。", location)
        names = [_string(item, f"{location}/{index}") for index, item in enumerate(references)]
        if len(names) != len(set(names)):
            _invalid("モデル参照を重複させることはできません。", location)
        for index, name in enumerate(names):
            if name not in models:
                _invalid("存在する model を参照してください。", f"{location}/{index}")


def _validate_limits(value: Any) -> None:
    limits = _object(value, "/limits")
    _keys(limits, LIMIT_KEYS, "/limits")
    for key in LIMIT_KEYS:
        _positive_integer(limits[key], f"/limits/{key}")


def _validate_request(value: Any) -> None:
    request = _object(value, "/request")
    _keys(request, {"timeout_seconds", "retry_attempts"}, "/request")
    _positive_integer(request["timeout_seconds"], "/request/timeout_seconds")
    _positive_integer(request["retry_attempts"], "/request/retry_attempts")


def _keys(value: dict[str, Any], required: set[str], location: str, *, optional: set[str] | None = None) -> None:
    allowed = required | (optional or set())
    missing = required - value.keys()
    if missing:
        _invalid("必須キーがありません。", f"{location.rstrip('/')}/{_pointer(sorted(missing)[0])}")
    unknown = value.keys() - allowed
    if unknown:
        _invalid("未知のキーです。", f"{location.rstrip('/')}/{_pointer(sorted(unknown)[0])}")


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid("値のまとまり(object)である必要があります。", location)
    return value


def _list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        _invalid("値の並び(array)である必要があります。", location)
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str):
        _invalid("文字列である必要があります。", location)
    return value


def _integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid("整数である必要があります。", location)
    return value


def _positive_integer(value: Any, location: str) -> int:
    integer = _integer(value, location)
    if integer < 1:
        _invalid("1 以上である必要があります。", location)
    return integer


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _invalid(reason: str, location: str) -> Never:
    raise StoryPipelineError(
        reason,
        location,
        "story-pipeline-config.jsonc の該当箇所を修正してください。",
        EXIT_CONFIG,
    )
