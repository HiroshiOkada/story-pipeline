"""dotenv と provider 認証環境の副作用のない検証。"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import re
from typing import Any

from story_pipeline.errors import StoryPipelineError
from story_pipeline.validation import IssueCollector


ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_environment(
    config: dict[str, Any],
    *,
    process_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """プロセス環境を優先し、dotenv を設定順に副作用なくマージする。"""
    values = dict(os.environ if process_environment is None else process_environment)
    for configured_path in config["dotenv"]["files"]:
        path = Path(configured_path)
        if not path.exists():
            continue
        if not path.is_file():
            raise _environment_error("dotenv パスが通常ファイルではありません", path)
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise _environment_error("dotenv ファイルを UTF-8 で読み取れません", path) from error
        _merge_values(source, values)
    return values


def require_api_key(
    provider_name: str,
    provider: dict[str, Any],
    environment: Mapping[str, str],
) -> str:
    """provider の API key を取得し、空値を認証エラーとして扱う。"""
    variable = provider["api_key_env"]
    value = environment.get(variable, "")
    if not value.strip():
        raise StoryPipelineError(
            f"provider {provider_name} の API キーが空または未定義です",
            variable,
            "環境変数または設定された dotenv ファイルを確認してください",
            4,
        )
    return value


def validate_environment(
    config: dict[str, Any] | None,
    collector: IssueCollector,
    *,
    process_environment: Mapping[str, str] | None = None,
) -> None:
    """設定順に dotenv を読み、必要な API key 名の存在だけを検査する。"""
    if config is None:
        return
    values = dict(os.environ if process_environment is None else process_environment)
    for configured_path in config["dotenv"]["files"]:
        path = Path(configured_path)
        if not path.exists():
            collector.warning("DOTENV_FILE_MISSING", "設定された dotenv ファイルがありません", str(path))
            continue
        if not path.is_file():
            collector.error("DOTENV_PATH_INVALID", "dotenv パスが通常ファイルではありません", str(path))
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            collector.error("DOTENV_FILE_UNREADABLE", "dotenv ファイルを UTF-8 で読み取れません", str(path))
            continue
        _merge_dotenv(source, path, values, collector)

    for provider_name, provider in config["providers"].items():
        variable = provider["api_key_env"]
        if not values.get(variable, "").strip():
            collector.error(
                "API_KEY_ENV_MISSING",
                f"provider {provider_name} に必要な環境変数が空または未定義です",
                variable,
            )


def _merge_dotenv(
    source: str,
    path: Path,
    values: dict[str, str],
    collector: IssueCollector,
) -> None:
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if match is None:
            collector.warning(
                "DOTENV_LINE_INVALID",
                "dotenv の代入形式として解釈できません",
                f"{path}:{line_number}",
            )
            continue
        name, raw_value = match.groups()
        if name in values:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value


def _merge_values(source: str, values: dict[str, str]) -> None:
    """不正行は値として採用せず、既存値を上書きしない。"""
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if match is None:
            continue
        name, raw_value = match.groups()
        if name in values:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value


def _environment_error(reason: str, path: Path) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        str(path),
        "dotenv ファイルの状態とアクセス権を確認してください",
        4,
    )
