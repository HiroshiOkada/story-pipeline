"""作品状態の厳密な読み込みと検証。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Never

from story_pipeline.errors import StoryPipelineError


EXIT_CONFIG = 4
PHASES = {
    "concept", "foundation", "plotting", "episode_planning", "drafting",
    "chapter_revision", "final_revision", "completed",
}
STATE_KEYS = {
    "schema_version", "phase", "next_chapter", "next_episode",
    "completed_chapters", "completed_episodes", "current_chapter",
    "pending_reviews", "pending_decisions", "last_request", "active_request",
    "updated_at",
}
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def load_state(root: Path) -> dict[str, Any]:
    """`state.json` を読み、スキーマと内部整合性を検証する。"""
    path = root / ".story-pipeline" / "state.json"
    try:
        source = path.read_text(encoding="utf-8")
        state = json.loads(source, object_pairs_hook=_without_duplicate_keys)
    except (OSError, UnicodeError) as error:
        _invalid("状態ファイルを読み取れません。", str(path), error)
    except (json.JSONDecodeError, ValueError) as error:
        _invalid(f"状態 JSON が不正です: {error}", ".story-pipeline/state.json", error)
    return validate_state_data(state)


def validate_state_data(state: Any) -> dict[str, Any]:
    """メモリ上の state 値を読み込み時と同じ契約で検証する。"""
    if not isinstance(state, dict):
        _invalid("object である必要があります。", "/")
    _exact_keys(state, STATE_KEYS, "/")
    if _integer(state["schema_version"], "/schema_version") != 1:
        _invalid("1 を指定してください。", "/schema_version")
    phase = _string(state["phase"], "/phase")
    if phase not in PHASES:
        _invalid("定義済み phase を指定してください。", "/phase")
    next_chapter = _number(state["next_chapter"], "/next_chapter")
    next_episode = _number(state["next_episode"], "/next_episode")
    chapters = _number_sequence(state["completed_chapters"], "/completed_chapters")
    episodes = _number_sequence(state["completed_episodes"], "/completed_episodes")
    if chapters and next_chapter <= chapters[-1]:
        _invalid("完了済み最大番号より大きい必要があります。", "/next_chapter")
    if episodes and next_episode <= episodes[-1]:
        _invalid("完了済み最大番号より大きい必要があります。", "/next_episode")
    _nullable_number(state["current_chapter"], "/current_chapter")
    _validate_reviews(state["pending_reviews"])
    _validate_decisions(state["pending_decisions"])
    _nullable_number(state["last_request"], "/last_request", allow_zero=True)
    _nullable_number(state["active_request"], "/active_request", allow_zero=True)
    _timestamp(state["updated_at"], "/updated_at")
    if phase == "completed":
        if state["pending_reviews"]:
            _invalid("completed では空である必要があります。", "/pending_reviews")
        if state["pending_decisions"]:
            _invalid("completed では空である必要があります。", "/pending_decisions")
        if state["active_request"] is not None:
            _invalid("completed では null である必要があります。", "/active_request")
    return state


def _validate_reviews(value: Any) -> None:
    reviews = _array(value, "/pending_reviews")
    for index, item in enumerate(reviews):
        location = f"/pending_reviews/{index}"
        review = _object(item, location)
        _exact_keys(review, {"target_type", "target_number", "reason"}, location)
        target_type = _string(review["target_type"], f"{location}/target_type")
        if target_type not in {"chapter", "episode", "novel"}:
            _invalid("chapter、episode、novel のいずれかが必要です。", f"{location}/target_type")
        if target_type == "novel":
            if review["target_number"] is not None:
                _invalid("novel では null である必要があります。", f"{location}/target_number")
        else:
            _number(review["target_number"], f"{location}/target_number")
        _string(review["reason"], f"{location}/reason")


def _validate_decisions(value: Any) -> None:
    decisions = _array(value, "/pending_decisions")
    identifiers: set[str] = set()
    for index, item in enumerate(decisions):
        location = f"/pending_decisions/{index}"
        decision = _object(item, location)
        _exact_keys(decision, {"id", "request", "question", "reason", "choices", "created_at"}, location)
        identifier = _string(decision["id"], f"{location}/id")
        if not identifier or identifier in identifiers:
            _invalid("空でない一意の ID が必要です。", f"{location}/id")
        identifiers.add(identifier)
        _number(decision["request"], f"{location}/request", allow_zero=True)
        _string(decision["question"], f"{location}/question")
        _string(decision["reason"], f"{location}/reason")
        choices = _array(decision["choices"], f"{location}/choices")
        if not choices:
            _invalid("1 件以上必要です。", f"{location}/choices")
        for choice_index, choice in enumerate(choices):
            _string(choice, f"{location}/choices/{choice_index}")
        _timestamp(decision["created_at"], f"{location}/created_at")


def _number_sequence(value: Any, location: str) -> list[int]:
    items = _array(value, location)
    numbers = [_number(item, f"{location}/{index}") for index, item in enumerate(items)]
    if numbers != sorted(set(numbers)):
        _invalid("昇順かつ一意である必要があります。", location)
    return numbers


def _nullable_number(value: Any, location: str, *, allow_zero: bool = False) -> int | None:
    return None if value is None else _number(value, location, allow_zero=allow_zero)


def _number(value: Any, location: str, *, allow_zero: bool = False) -> int:
    number = _integer(value, location)
    minimum = 0 if allow_zero else 1
    if not minimum <= number <= 9999:
        _invalid(f"{minimum}..9999 の範囲である必要があります。", location)
    return number


def _timestamp(value: Any, location: str) -> str:
    timestamp = _string(value, location)
    if not RFC3339_UTC.fullmatch(timestamp):
        _invalid("UTC RFC 3339 形式が必要です。", location)
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        _invalid("有効な日時が必要です。", location, error)
    return timestamp


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重複したキーです: {key}")
        result[key] = value
    return result


def _exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    key = sorted(missing or unknown)[0] if missing or unknown else None
    if key is not None:
        kind = "必須キーがありません。" if missing else "未知のキーです。"
        _invalid(kind, f"{location.rstrip('/')}/{key}")


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid("object である必要があります。", location)
    return value


def _array(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        _invalid("array である必要があります。", location)
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str):
        _invalid("string である必要があります。", location)
    return value


def _integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid("integer である必要があります。", location)
    return value


def _invalid(reason: str, location: str, cause: BaseException | None = None) -> Never:
    error = StoryPipelineError(
        reason,
        location,
        ".story-pipeline/state.json の該当箇所を修正してください。",
        EXIT_CONFIG,
    )
    if cause is None:
        raise error
    raise error from cause
