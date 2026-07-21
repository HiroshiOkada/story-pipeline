"""LLM 応答を推測せずに検証する出力契約。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from story_pipeline.errors import StoryPipelineError


FENCE = re.compile(r"\A```([^\n`]*)\n([\s\S]*?)\n```[ \t]*\Z")


@dataclass(frozen=True, slots=True)
class FieldRule:
    types: tuple[type, ...]
    enum: frozenset[Any] | None = None
    minimum: int | None = None
    maximum: int | None = None


def validate_markdown(content: str, required_headings: tuple[str, ...] = ()) -> str:
    """単一 Markdown fence だけを除去し、空応答と必須見出しを拒否する。"""
    text = _strip_single_fence(content, {"", "md", "markdown"})
    if not text.strip():
        raise _format_error("Markdown 応答が空です")
    lines = {line.strip() for line in text.splitlines()}
    for heading in required_headings:
        if heading not in lines:
            raise _format_error(f"必須見出しがありません: {heading}")
    return text.strip() + "\n"


def parse_json_object(
    content: str,
    rules: dict[str, FieldRule],
    *,
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """単一 JSON fence を許し、object のキーと基本型を厳格に検証する。"""
    text = _strip_single_fence(content, {"", "json"})
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise _format_error(f"JSON 応答を解析できません: line {error.lineno} column {error.colno}") from None
    if not isinstance(value, dict):
        raise _format_error("JSON 応答は object である必要があります")
    required = set(rules) - set(optional)
    missing = required - value.keys()
    if missing:
        raise _format_error(f"JSON 応答に必須キーがありません: {sorted(missing)[0]}")
    unknown = value.keys() - rules.keys()
    if unknown:
        raise _format_error(f"JSON 応答に未知のキーがあります: {sorted(unknown)[0]}")
    for name, item in value.items():
        _validate_field(name, item, rules[name])
    return value


def validate_evaluation(content: str, *, completion: bool = False) -> dict[str, Any]:
    """共通評価形式と、必要な場合は完成判定を検証する。"""
    top_rules = {
        "decision": FieldRule((str,), frozenset({"accept", "revise", "awaiting_human"})),
        "summary": FieldRule((str,)),
        "issues": FieldRule((list,)),
        "scores": FieldRule((dict,)),
    }
    if completion:
        top_rules |= {"complete": FieldRule((bool,)), "reason": FieldRule((str,))}
    value = parse_json_object(content, top_rules)
    for index, issue in enumerate(value["issues"]):
        if not isinstance(issue, dict):
            raise _format_error(f"issues/{index} は object である必要があります")
        expected = {"severity", "category", "location", "evidence", "instruction"}
        if set(issue) != expected:
            raise _format_error(f"issues/{index} のキーが出力契約と一致しません")
        if issue["severity"] not in {"error", "warning", "note"}:
            raise _format_error(f"issues/{index}/severity が不正です")
        if any(not isinstance(issue[name], str) for name in expected - {"severity"}):
            raise _format_error(f"issues/{index} の文字列フィールドが不正です")
    if not value["scores"]:
        raise _format_error("scores は1件以上必要です")
    for name, score in value["scores"].items():
        if not isinstance(name, str) or type(score) is not int or not 1 <= score <= 5:
            raise _format_error("scores は名前と 1..5 の integer である必要があります")
    return value


def _strip_single_fence(content: str, allowed_languages: set[str]) -> str:
    if not isinstance(content, str):
        raise _format_error("応答本文が文字列ではありません")
    stripped = content.strip()
    if stripped.startswith("```"):
        match = FENCE.fullmatch(stripped)
        if match is None or match.group(1).strip().lower() not in allowed_languages:
            raise _format_error("応答全体を囲む単一の許可済みコード fence ではありません")
        return match.group(2)
    return stripped


def _validate_field(name: str, value: Any, rule: FieldRule) -> None:
    valid_type = any(
        type(value) is expected if expected in {int, bool} else isinstance(value, expected)
        for expected in rule.types
    )
    if not valid_type:
        raise _format_error(f"JSON フィールド {name} の型が不正です")
    if rule.enum is not None and value not in rule.enum:
        raise _format_error(f"JSON フィールド {name} の値が不正です")
    if rule.minimum is not None and value < rule.minimum:
        raise _format_error(f"JSON フィールド {name} が最小値未満です")
    if rule.maximum is not None and value > rule.maximum:
        raise _format_error(f"JSON フィールド {name} が最大値を超えています")


def _format_error(reason: str) -> StoryPipelineError:
    return StoryPipelineError(reason, "LLM response", "応答を修正指示付きで再生成してください", 7)
