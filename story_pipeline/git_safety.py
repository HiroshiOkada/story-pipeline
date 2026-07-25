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
    normalize_git_path,
)
from story_pipeline.validation import IssueCollector
from story_pipeline.scaffold import SCAFFOLD_FILE_PATHS


@dataclass(frozen=True, slots=True)
class GitPreflight:
    entries: tuple[WorktreeEntry, ...]
    configured_dotenv: frozenset[str]


def inspect_run_preconditions(root: Path, config: dict[str, Any]) -> GitPreflight:
    """利用者の状態を変えずに `run` の Git 前提を検査する。"""
    validate_run_repository(root)
    dotenv = _configured_dotenv_paths(root, config)
    entries = read_worktree(root)
    for entry in entries:
        path = entry.normalized_path()
        if entry.kind == "ignored":
            continue
        if entry.kind == "unmerged":
            raise _git_error(
                "競合を解消する必要があります",
                path,
                "git status で競合ファイルを確認し、解消してマージ等の操作を完了してから再実行してください",
            )
        if entry.index_status not in {".", "?"}:
            raise _git_error(
                "stage 済み変更があります",
                path,
                "自分の変更を commit するか git restore --staged で stage を取り消してから再実行してください",
            )
        if entry.kind == "untracked" and classify_path(path, dotenv) == "managed":
            raise _git_error(
                "未追跡の CLI 管理ファイルがあります",
                path,
                "CLI が管理する名前のファイルです。削除または別名に変更してから再実行してください",
            )
    _inspect_index_flags(root, dotenv)
    return GitPreflight(tuple(entries), frozenset(dotenv))


def inspect_initial_repository(root: Path) -> None:
    """既存の空 repository を変更せず初期化前提を検査する。"""
    validate_run_repository(root)
    entries = read_worktree(root)
    if entries:
        raise _git_error(
            "初期化前の Git repository に差分があります",
            entries[0].normalized_path(),
        )
    _inspect_index_flags(root, set())


def validate_run_repository(root: Path) -> None:
    """ロック取得前に Git repository と進行中操作だけを検証する。"""
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


def current_commit(root: Path) -> str:
    """現在の HEAD object ID を安全な開始境界として返す。"""
    commit = _git_text(root, ["rev-parse", "HEAD"])
    if commit is None:
        raise _git_error("現在の commit を確認できません", root)
    return commit


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


def commit_start_inputs(
    root: Path,
    request_number: int,
    paths: tuple[str, ...],
    additional_materials: tuple[str, ...] = (),
) -> str | None:
    """要求・設定・検査済み追加資料だけを開始時コミットへ保存する。"""
    body = tuple(f"Additional material: {path}" for path in additional_materials)
    return commit_explicit_paths(
        root,
        paths,
        f"Record request {request_number:04d} input",
        body,
    )


def commit_initial_scaffold(root: Path) -> str:
    """検証済み scaffold の既知ファイルだけを初期 commit に保存する。"""
    commit = commit_explicit_paths(
        root,
        SCAFFOLD_FILE_PATHS,
        "Initialize story project",
    )
    if commit is None:
        raise _git_error("初期 commit の対象がありません", root)
    return commit


def commit_run_outputs(
    root: Path,
    request_number: int,
    status: str,
    paths: tuple[str, ...],
    body: tuple[str, ...] = (),
) -> str | None:
    """今回変更した管理ファイルだけを終了時コミットへ保存する。"""
    if status not in {"completed", "failed", "awaiting-human"}:
        raise ValueError("終了 status が不正です")
    for path in paths:
        if classify_path(path) != "managed":
            raise _git_error("終了時コミットに管理対象外パスが指定されました", path)
    return commit_explicit_paths(
        root,
        paths,
        f"Complete request {request_number:04d}: {status}",
        body,
    )


def commit_explicit_paths(
    root: Path,
    paths: tuple[str, ...],
    subject: str,
    body: tuple[str, ...] = (),
) -> str | None:
    """明示したファイル集合だけを stage し、完全一致を確認して commit する。"""
    expected = _validate_explicit_paths(paths)
    if not expected:
        return None
    result = _git(root, ["add", "--", *sorted(expected)])
    if result is None or result.returncode != 0:
        raise _git_error("指定ファイルを stage できません", sorted(expected)[0])
    staged = _staged_paths(root)
    if staged != expected:
        _unstage_our_paths(root, expected)
        raise _git_error("stage 済みパスが予定集合と一致しません", root)
    arguments = ["commit", "-m", subject]
    for paragraph in body:
        arguments.extend(["-m", paragraph])
    result = _git(root, arguments)
    if result is None or result.returncode != 0:
        _unstage_our_paths(root, expected)
        raise _git_error("Git commit に失敗しました", root)
    commit = _git_text(root, ["rev-parse", "HEAD"])
    if commit is None:
        raise _git_error("作成した commit を確認できません", root)
    return commit


def _validate_explicit_paths(paths: tuple[str, ...]) -> set[str]:
    normalized: set[str] = set()
    for value in paths:
        path = normalize_git_path(value)
        if path is None or path == "." or value.endswith("/"):
            raise _git_error("commit 対象に安全でないパスが指定されました", value)
        normalized.add(path)
    return normalized


def _staged_paths(root: Path) -> set[str]:
    result = _git(root, ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACDMRTUXB"])
    if result is None or result.returncode != 0:
        raise _git_error("stage 済みパスを確認できません", root)
    return {
        path
        for value in result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if value and (path := normalize_git_path(value)) is not None
    }


def _unstage_our_paths(root: Path, paths: set[str]) -> None:
    if _git_text(root, ["rev-parse", "--verify", "HEAD"]) is None:
        _git(root, ["rm", "--cached", "-q", "--ignore-unmatch", "--", *sorted(paths)])
        return
    _git(root, ["restore", "--staged", "--", *sorted(paths)])


def _inspect_index_flags(root: Path, configured_dotenv: set[str]) -> None:
    result = _git(root, ["ls-files", "-v"])
    if result is not None and result.returncode == 0:
        for line in result.stdout.decode("utf-8", errors="surrogateescape").splitlines():
            path = normalize_git_path(line[2:]) if len(line) > 2 else None
            if (
                path is not None
                and classify_path(path, configured_dotenv) in {"managed", "human"}
                and (line[0].islower() or line[0] == "S")
            ):
                raise _git_error("保護対象パスに特殊な index flag が設定されています", path)
    stage = _git(root, ["ls-files", "--stage"])
    if stage is not None and stage.returncode == 0:
        for line in stage.stdout.decode("utf-8", errors="surrogateescape").splitlines():
            fields = line.split(maxsplit=2)
            path = normalize_git_path(line.split("\t", 1)[1]) if "\t" in line else None
            if (
                len(fields) == 3
                and fields[1] == "0" * 40
                and path is not None
                and classify_path(path, configured_dotenv) in {"managed", "human"}
            ):
                raise _git_error("intent-to-add が設定されています", path)


def _entry_signatures(entries: tuple[WorktreeEntry, ...] | list[WorktreeEntry]) -> dict[str, tuple[str, str, str]]:
    return {
        entry.normalized_path(): (entry.index_status, entry.worktree_status, entry.kind)
        for entry in entries
        if not entry.rename_origin
    }


def _git_error(reason: str, location: str | Path, action: str | None = None) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        str(location),
        action or "Git の状態を確認してから再実行してください",
        5,
    )
