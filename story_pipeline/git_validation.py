"""Git リポジトリと作業ツリーの副作用のない検証。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Literal

from story_pipeline.errors import StoryPipelineError
from story_pipeline.validation import IssueCollector


MANAGED_FILE = re.compile(
    r"^(?:concept|world|characters|plot|style|canon)\.md$|"
    r"^(?:chapters|episode_plans|episodes)/[0-9]{4}\.md$|"
    r"^requests/[0-9]{4}_agent\.md$|"
    r"^\.story-pipeline/state\.json$|"
    r"^\.story-pipeline/runs/[0-9]{4}\.json$"
    r"|^\.story-pipeline/checkpoints/[0-9]{4}/draft\.json$"
)
HUMAN_INPUT = re.compile(r"^requests/[0-9]{4}\.md$|^story-pipeline-config\.jsonc$")
INFRASTRUCTURE = {".gitignore"}
TEMPORARY = {".env", ".story-pipeline/run.lock"}


@dataclass(frozen=True, slots=True)
class WorktreeEntry:
    path: str
    index_status: str
    worktree_status: str
    kind: str
    rename_origin: bool = False

    def normalized_path(self) -> str:
        """Git の出力パスを検証して正規化する。"""
        normalized = normalize_git_path(self.path)
        if normalized is None:
            raise StoryPipelineError(
                "Git が安全でないパスを報告しました",
                self.path,
                "リポジトリの状態とパス名を確認してください",
                5,
            )
        return normalized


PathClassification = Literal["managed", "human", "temporary", "unexpected"]


def validate_git(
    root: Path,
    collector: IssueCollector,
    config: dict[str, Any] | None = None,
) -> None:
    """Git 状態を変更せず、run の安全要件に関係する問題を列挙する。"""
    top_level = _git_text(root, ["rev-parse", "--show-toplevel"])
    if top_level is None:
        collector.error("GIT_REPOSITORY_MISSING", "Git ワークツリーを特定できません", ".git")
        return
    try:
        git_root = Path(top_level).resolve(strict=True)
    except OSError:
        collector.error("GIT_ROOT_INVALID", "Git ワークツリーのルートを解決できません", top_level)
        return
    if git_root != root:
        collector.error("GIT_ROOT_MISMATCH", "作品ルートと Git ワークツリーのルートが一致しません", str(git_root))

    bare = _git_text(root, ["rev-parse", "--is-bare-repository"])
    if bare == "true":
        collector.error("GIT_BARE_REPOSITORY", "bare repository は使用できません", ".git")
    if _git_text(root, ["symbolic-ref", "-q", "HEAD"]) is None:
        collector.error("GIT_DETACHED_HEAD", "detached HEAD では実行できません", "HEAD")
    _validate_git_operation(root, collector)
    configured_dotenv = _configured_dotenv_paths(root, config)
    try:
        entries = read_worktree(root)
    except StoryPipelineError:
        collector.error("GIT_STATUS_FAILED", "Git 作業ツリーの状態を取得できません")
        entries = []
    for entry in entries:
        _validate_entry(entry, collector, configured_dotenv)
    protected_paths = TEMPORARY | configured_dotenv
    _validate_required_ignores(root, collector, protected_paths)
    _validate_temporary_tracking(root, collector, protected_paths)


def _validate_git_operation(root: Path, collector: IssueCollector) -> None:
    git_directory_text = _git_text(root, ["rev-parse", "--git-dir"])
    if git_directory_text is None:
        return
    git_directory = Path(git_directory_text)
    if not git_directory.is_absolute():
        git_directory = root / git_directory
    markers = {
        "MERGE_HEAD": "merge",
        "REBASE_HEAD": "rebase",
        "rebase-merge": "rebase",
        "rebase-apply": "rebase",
        "CHERRY_PICK_HEAD": "cherry-pick",
        "REVERT_HEAD": "revert",
        "BISECT_LOG": "bisect",
    }
    for marker, operation in markers.items():
        if (git_directory / marker).exists():
            collector.error("GIT_OPERATION_IN_PROGRESS", f"{operation} 操作中です", f".git/{marker}")


def read_worktree(root: Path) -> list[WorktreeEntry]:
    """porcelain v2 の NUL 区切り出力を読み、rename 元を含めて返す。"""
    result = _git(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching"])
    if result is None or result.returncode != 0:
        raise StoryPipelineError(
            "Git 作業ツリーの状態を取得できません",
            str(root),
            "Git リポジトリとアクセス権を確認してください",
            5,
        )
    records = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    entries: list[WorktreeEntry] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        prefix = record[0]
        if prefix in {"?", "!"}:
            path = record[2:]
            entries.append(WorktreeEntry(path, prefix, prefix, "untracked" if prefix == "?" else "ignored"))
            continue
        if prefix == "1":
            fields = record.split(" ", 8)
            if len(fields) == 9:
                entries.append(WorktreeEntry(fields[8], fields[1][0], fields[1][1], "tracked"))
            continue
        if prefix == "2":
            fields = record.split(" ", 9)
            if len(fields) == 10:
                entries.append(WorktreeEntry(fields[9], fields[1][0], fields[1][1], "tracked"))
                if index >= len(records) or not records[index]:
                    raise _invalid_porcelain_record(record)
                entries.append(
                    WorktreeEntry(records[index], fields[1][0], fields[1][1], "tracked", True)
                )
                index += 1
            continue
        if prefix == "u":
            fields = record.split(" ", 10)
            if len(fields) == 11:
                entries.append(WorktreeEntry(fields[10], "U", "U", "unmerged"))
        raise _invalid_porcelain_record(record)
    return entries


def _invalid_porcelain_record(record: str) -> StoryPipelineError:
    return StoryPipelineError(
        "Git 状態の出力形式を解釈できません",
        record[:80],
        "Git のバージョンとリポジトリの状態を確認してください",
        5,
    )


def _validate_entry(
    entry: WorktreeEntry,
    collector: IssueCollector,
    configured_dotenv: set[str],
) -> None:
    path = normalize_git_path(entry.path)
    if path is None:
        collector.error("GIT_PATH_INVALID", "Git が安全でないパスを報告しました", entry.path)
        return
    classification = classify_path(path, configured_dotenv)
    if entry.kind == "ignored":
        return
    if entry.kind == "unmerged":
        collector.error("GIT_UNMERGED_PATH", "競合を解消する必要があります", path)
        return
    if entry.index_status not in {".", "?"}:
        collector.error("GIT_STAGED_CHANGE", "stage 済み変更があります", path)
    if entry.kind == "untracked":
        if classification == "managed":
            collector.error("UNTRACKED_MANAGED_FILE", "CLI 管理ファイルが未追跡です", path)
        elif classification == "unexpected":
            collector.warning("UNTRACKED_UNKNOWN_FILE", "Story Pipeline の管理対象ではありません", path)
        return
    if entry.worktree_status != ".":
        if classification == "managed":
            collector.error("MODIFIED_MANAGED_FILE", "CLI 管理ファイルが直接変更されています", path)
        elif classification == "unexpected":
            collector.warning("MODIFIED_UNKNOWN_FILE", "管理対象外ファイルに変更があります", path)


def _validate_required_ignores(
    root: Path, collector: IssueCollector, protected_paths: set[str]
) -> None:
    for path in sorted(protected_paths):
        result = _git(root, ["check-ignore", "-q", "--no-index", "--", path])
        if result is None or result.returncode != 0:
            collector.error("GITIGNORE_REQUIRED_PATTERN", "必須の除外パスが ignore されません", path)


def _validate_temporary_tracking(
    root: Path, collector: IssueCollector, protected_paths: set[str]
) -> None:
    for path in sorted(protected_paths):
        result = _git(root, ["ls-files", "--error-unmatch", "--", path])
        if result is not None and result.returncode == 0:
            collector.error("TRACKED_TEMPORARY_FILE", "秘密またはロック用ファイルが追跡されています", path)


def classify_path(path: str, configured_dotenv: set[str] | None = None) -> PathClassification:
    """正規化済み相対パスを排他的な安全分類へ割り当てる。"""
    dotenv_paths = set() if configured_dotenv is None else configured_dotenv
    if MANAGED_FILE.fullmatch(path):
        return "managed"
    if HUMAN_INPUT.fullmatch(path) or path in INFRASTRUCTURE:
        return "human"
    if path in TEMPORARY or path in dotenv_paths:
        return "temporary"
    return "unexpected"


def _configured_dotenv_paths(
    root: Path, config: dict[str, Any] | None
) -> set[str]:
    if config is None:
        return set()
    paths: set[str] = set()
    for configured in config["dotenv"]["files"]:
        path = Path(configured)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative and relative != ".":
            paths.add(relative)
    return paths


def normalize_git_path(value: str) -> str | None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        return None
    return path.as_posix()


def _git_text(root: Path, arguments: list[str]) -> str | None:
    result = _git(root, arguments)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip()


def _git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
