"""処理対象要求の非破壊な探索と本文検査。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal

from story_pipeline.errors import StoryPipelineError


REQUEST_FILE = re.compile(r"^([0-9]{4})\.md$")
HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
MARKDOWN_ONLY = re.compile(r"^[\s#>*_`~\-]+$")


@dataclass(frozen=True, slots=True)
class SelectedRequest:
    number: int
    relative_path: str
    content: str
    mode: Literal["pending", "resume"]


def select_request(root: Path, state: dict[str, Any]) -> SelectedRequest | None:
    """active request を優先し、それ以外は最若の未処理要求を返す。"""
    active = state["active_request"]
    if active is not None:
        run_path = root / ".story-pipeline" / "runs" / f"{active:04d}.json"
        if not _regular_file_without_symlink(run_path):
            raise _selection_error(
                "再開対象の実行記録が安全な通常ファイルではありません",
                f".story-pipeline/runs/{active:04d}.json",
                4,
            )
        return _load_selected(root, active, "resume")

    requests = root / "requests"
    if not _directory_without_symlink(requests):
        raise _selection_error("要求ディレクトリが安全な通常ディレクトリではありません", "requests", 4)
    pending: list[int] = []
    try:
        entries = list(requests.iterdir())
    except OSError as error:
        raise _selection_error("要求ディレクトリを読み取れません", "requests", 4) from error
    for entry in entries:
        match = REQUEST_FILE.fullmatch(entry.name)
        if match is None:
            continue
        number = int(match.group(1))
        if not _regular_file_without_symlink(entry):
            raise _selection_error("要求が安全な通常ファイルではありません", f"requests/{entry.name}", 4)
        run_path = root / ".story-pipeline" / "runs" / f"{number:04d}.json"
        if run_path.exists() or run_path.is_symlink():
            if not _regular_file_without_symlink(run_path):
                raise _selection_error(
                    "実行記録が安全な通常ファイルではありません",
                    f".story-pipeline/runs/{number:04d}.json",
                    4,
                )
            continue
        pending.append(number)
    if not pending:
        return None
    return _load_selected(root, min(pending), "pending")


def has_meaningful_request_content(content: str) -> bool:
    """テンプレート見出し、コメント、装飾記号以外の記述があるか判定する。"""
    without_comments = HTML_COMMENT.sub("", content)
    for raw_line in without_comments.splitlines():
        line = raw_line.strip()
        if not line or re.fullmatch(r"#{1,6}\s+.*", line):
            continue
        if not MARKDOWN_ONLY.fullmatch(line):
            return True
    return False


def _load_selected(
    root: Path, number: int, mode: Literal["pending", "resume"]
) -> SelectedRequest:
    relative = f"requests/{number:04d}.md"
    path = root / relative
    if not _regular_file_without_symlink(path):
        raise _selection_error("処理対象要求が安全な通常ファイルではありません", relative, 4)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _selection_error("処理対象要求を UTF-8 で読み取れません", relative, 4) from error
    if not has_meaningful_request_content(content):
        raise _selection_error(
            "処理対象要求に具体的な内容がありません",
            relative,
            8,
            "テンプレートのコメント部分へ要求を記入してください",
        )
    return SelectedRequest(number, relative, content, mode)


def _regular_file_without_symlink(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _directory_without_symlink(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _selection_error(
    reason: str,
    location: str,
    code: int,
    action: str = "要求と実行記録の状態を確認してから再実行してください",
) -> StoryPipelineError:
    return StoryPipelineError(reason, location, action, code)
