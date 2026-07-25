"""仕様で許可された範囲だけを扱う JSONC 読み込み。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from story_pipeline.errors import StoryPipelineError


EXIT_CONFIG = 4


def load_jsonc(path: Path) -> Any:
    """UTF-8 の JSONC ファイルを読み、重複キーを拒否する。"""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise StoryPipelineError(
            "設定ファイルを読み取れません。",
            str(path),
            "UTF-8 のファイルと読み取り権限を確認してください。",
            EXIT_CONFIG,
        ) from error
    try:
        return json.loads(
            _remove_trailing_commas(_replace_comments(source)),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except json.JSONDecodeError as error:
        raise StoryPipelineError(
            f"設定ファイルの JSON 書式が不正です({error.lineno} 行目 {error.colno} 文字目)",
            str(path),
            "引用符・カンマ・括弧の対応を確認してください。",
            EXIT_CONFIG,
        ) from error
    except ValueError as error:
        raise StoryPipelineError(
            f"設定ファイルの内容が不正です: {error}",
            str(path),
            "コメントの閉じ忘れやキーの重複がないか確認してください。",
            EXIT_CONFIG,
        ) from error


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重複したキーです: {key}")
        result[key] = value
    return result


def _replace_comments(source: str) -> str:
    output = list(source)
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        character = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end == -1 else end
            _blank_non_newlines(output, index, end)
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                raise ValueError("ブロックコメントが閉じられていません")
            end += 2
            _blank_non_newlines(output, index, end)
            index = end
            continue
        index += 1
    return "".join(output)


def _blank_non_newlines(output: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if output[index] not in "\r\n":
            output[index] = " "


def _remove_trailing_commas(source: str) -> str:
    output = list(source)
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        character = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character == ",":
            lookahead = index + 1
            while lookahead < len(source) and source[lookahead].isspace():
                lookahead += 1
            if lookahead < len(source) and source[lookahead] in "}]":
                output[index] = " "
        index += 1
    return "".join(output)
