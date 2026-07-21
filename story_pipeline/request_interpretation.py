"""人間要求を表す構造化 JSON の厳格な出力契約。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from story_pipeline.errors import StoryPipelineError
from story_pipeline.llm_output import FieldRule, parse_json_object


REQUEST_KINDS = frozenset({"create", "continue", "modify", "add", "reconsider", "answer", "mixed"})


@dataclass(frozen=True, slots=True)
class DecisionAnswer:
    id: str
    answer: str


@dataclass(frozen=True, slots=True)
class RequestInterpretation:
    kind: str
    summary: str
    targets: tuple[str, ...]
    required_conditions: tuple[str, ...]
    prohibited_changes: tuple[str, ...]
    additional_material: tuple[str, ...]
    decision_answers: tuple[DecisionAnswer, ...]
    ambiguities: tuple[str, ...]
    requested_units: int
    requested_until: str | None


def parse_request_interpretation(content: str, request_source: str) -> RequestInterpretation:
    """解釈応答を検証し、要求にない明示対象の追加を拒否する。"""
    rules = {
        "kind": FieldRule((str,), REQUEST_KINDS),
        "summary": FieldRule((str,)),
        "targets": FieldRule((list,)),
        "required_conditions": FieldRule((list,)),
        "prohibited_changes": FieldRule((list,)),
        "additional_material": FieldRule((list,)),
        "decision_answers": FieldRule((list,)),
        "ambiguities": FieldRule((list,)),
        "requested_units": FieldRule((int,), minimum=1, maximum=9999),
        "requested_until": FieldRule((str, type(None))),
    }
    value = parse_json_object(content, rules)
    summary = _nonempty_string(value["summary"], "summary")
    targets = _string_list(value["targets"], "targets")
    required = _string_list(value["required_conditions"], "required_conditions")
    prohibited = _string_list(value["prohibited_changes"], "prohibited_changes")
    materials = _string_list(value["additional_material"], "additional_material")
    ambiguities = _string_list(value["ambiguities"], "ambiguities")
    for index, target in enumerate(targets):
        if target not in request_source:
            raise _interpretation_error(
                f"要求本文に明示されていない対象です: targets/{index}"
            )
    for index, material in enumerate(materials):
        _relative_path(material, f"additional_material/{index}")
        if material not in request_source:
            raise _interpretation_error(
                f"要求本文に明示されていない追加資料です: additional_material/{index}"
            )
    answers = _decision_answers(value["decision_answers"])
    requested_until = value["requested_until"]
    if isinstance(requested_until, str):
        requested_until = _nonempty_string(requested_until, "requested_until")
    return RequestInterpretation(
        value["kind"],
        summary,
        targets,
        required,
        prohibited,
        materials,
        answers,
        ambiguities,
        value["requested_units"],
        requested_until,
    )


def _decision_answers(value: list[Any]) -> tuple[DecisionAnswer, ...]:
    answers: list[DecisionAnswer] = []
    identifiers: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"id", "answer"}:
            raise _interpretation_error(f"decision_answers/{index} の形式が不正です")
        identifier = _nonempty_string(item["id"], f"decision_answers/{index}/id")
        answer = _nonempty_string(item["answer"], f"decision_answers/{index}/answer")
        if identifier in identifiers:
            raise _interpretation_error(f"判断回答 ID が重複しています: {identifier}")
        identifiers.add(identifier)
        answers.append(DecisionAnswer(identifier, answer))
    return tuple(answers)


def _string_list(value: list[Any], location: str) -> tuple[str, ...]:
    result = tuple(_nonempty_string(item, f"{location}/{index}") for index, item in enumerate(value))
    if len(result) != len(set(result)):
        raise _interpretation_error(f"{location} に重複があります")
    return result


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _interpretation_error(f"{location} は空でない string である必要があります")
    return value.strip()


def _relative_path(value: str, location: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or value.endswith("/"):
        raise _interpretation_error(f"{location} は安全な作品ルート相対ファイルである必要があります")


def _interpretation_error(reason: str) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        "request interpretation",
        "要求解釈を修正指示付きで再生成してください",
        7,
    )
