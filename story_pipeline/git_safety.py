"""`run` が使用する非破壊 Git 事前検査と限定復元。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from story_pipeline.errors import StoryPipelineError
from story_pipeline.git_validation import (
    WorktreeEntry,
    _configured_dotenv_paths,
    _git,
    _git_text,
    _validate_git_operation,
    classify_path,
    read_worktree,
)
from story_pipeline.validation import IssueCollector


@dataclass(frozen=True, slots=True)
class GitPreflight:
    entries: tuple[WorktreeEntry, ...]
    configured_dotenv: frozenset[str]


def inspect_run_preconditions(root: Path, config: dict[str, Any]) -> GitPreflight:
    """利用者の状態を変えずに `run` の Git 前提を検査する。"""
    top_level = _git_text(root, ["rev-parse", "--show-toplevel"])
    if top_level is None or Path(top_level).resolve() != root.resolve():
        raise _git_error("作品ルートと Git ワークツリーのルートが一致しません", root)
    if _git_text(root, ["rev-parse", "--is-bare-repository"]) == "true":
        raise _git_error("bare repository は使用できません", root / ".git")
    if _git_text(root, ["symbolic-ref", "-q", "HEAD"]) is None:
        raise _git_error("detached HEAD では実行できません", root / ".git/HEAD")

    operations = IssueCollector()
    _validate_git_operation(root, operations)
    if operations.error_count:
        issue = operations.issues[0]
        raise _git_error(issue.message, issue.location or root)

    dotenv = _configured_dotenv_paths(root, config)
    entries = read_worktree(root)
    for entry in entries:
        path = entry.normalized_path()
        if entry.kind == "unmerged":
            raise _git_error("競合を解消する必要があります", path)
        if entry.index_status not in {".", "?"}:
            raise _git_error("stage 済み変更があります", path)
        if entry.kind == "untracked" and classify_path(path, dotenv) == "managed":
            raise _git_error("未追跡の CLI 管理ファイルがあります", path)
    _inspect_index_flags(root, entries, dotenv)
    return GitPreflight(tuple(entries), frozenset(dotenv))


def restore_managed_files(root: Path, preflight: GitPreflight, output: TextIO) -> tuple[str, ...]:
    """変更済み tracked 管理ファイルだけを明示パスで復元する。"""
    targets = tuple(
        sorted(
            {
                entry.normalized_path()
                for entry in preflight.entries
                if not entry.rename_origin
                and entry.kind == "tracked"
                and entry.worktree_status != "."
                and classify_path(entry.normalized_path(), set(preflight.configured_dotenv)) == "managed"
            }
        )
    )
    if not targets:
        return ()
    before = _entry_signatures(preflight.entries)
    for path in targets:
        print(f"Restoring managed file: {path}", file=output)
    result = _git(root, ["restore", "--worktree", "--source=HEAD", "--", *targets])
    if result is None or result.returncode != 0:
        raise _git_error("CLI 管理ファイルを復元できません", targets[0])
    after_entries = read_worktree(root)
    after = _entry_signatures(after_entries)
    target_set = set(targets)
    before_unrelated = {key: value for key, value in before.items() if key not in target_set}
    after_unrelated = {key: value for key, value in after.items() if key not in target_set}
    if before_unrelated != after_unrelated:
        raise _git_error("復元中に対象外の Git 状態が変化しました", root)
    remaining = target_set & set(after)
    if remaining:
        raise _git_error("CLI 管理ファイルの変更が復元後も残っています", sorted(remaining)[0])
    return targets


def _inspect_index_flags(
    root: Path, entries: list[WorktreeEntry], configured_dotenv: set[str]
) -> None:
    protected = {
        entry.normalized_path()
        for entry in entries
        if classify_path(entry.normalized_path(), configured_dotenv) in {"managed", "human"}
    }
    result = _git(root, ["ls-files", "-v", "--", *sorted(protected)]) if protected else None
    if result is not None and result.returncode == 0:
        for line in result.stdout.decode("utf-8", errors="surrogateescape").splitlines():
            if len(line) > 2 and (line[0].islower() or line[0] == "S"):
                raise _git_error("保護対象パスに特殊な index flag が設定されています", line[2:])
    stage = _git(root, ["ls-files", "--stage", "--", *sorted(protected)]) if protected else None
    if stage is not None and stage.returncode == 0:
        for line in stage.stdout.decode("utf-8", errors="surrogateescape").splitlines():
            fields = line.split(maxsplit=2)
            if len(fields) == 3 and fields[1] == "0" * 40:
                raise _git_error("intent-to-add が設定されています", line.split("\t", 1)[1])


def _entry_signatures(entries: tuple[WorktreeEntry, ...] | list[WorktreeEntry]) -> dict[str, tuple[str, str, str]]:
    return {
        entry.normalized_path(): (entry.index_status, entry.worktree_status, entry.kind)
        for entry in entries
        if not entry.rename_origin
    }


def _git_error(reason: str, location: str | Path) -> StoryPipelineError:
    return StoryPipelineError(reason, str(location), "Git の状態を確認してから再実行してください", 5)
