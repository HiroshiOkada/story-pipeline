"""未記録情報を保全し、作品成果物から待機中の制作状態を再構築する。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import stat
from typing import Any, TextIO

from story_pipeline.errors import StoryPipelineError
from story_pipeline.git_safety import commit_explicit_paths, validate_run_repository
from story_pipeline.git_validation import TEMPORARY, read_worktree
from story_pipeline.persistence import atomic_write_json
from story_pipeline.run_lifecycle import utc_timestamp
from story_pipeline.state import validate_state_data
from story_pipeline.story_structure import StoryStructure, load_story_structure


NUMBERED_JSON = re.compile(r"^([0-9]{4})\.json$")
FOUNDATION_FILES = ("world.md", "characters.md", "style.md", "canon.md")


def recover_state_command(root: Path, abandon_active: bool, output: TextIO) -> int:
    """作業ツリーを保全して、成果物から新規要求を受理できる state を作る。"""
    if not abandon_active:
        raise _error(
            "active request と確認待ちを放棄する明示指定が必要です",
            "--abandon-active",
            "story-pipeline recover --abandon-active を実行してください",
            2,
        )
    validate_run_repository(root)
    lock = root / ".story-pipeline/run.lock"
    if lock.exists() or lock.is_symlink():
        raise _error(
            "実行ロックがある間は復旧できません",
            ".story-pipeline/run.lock",
            "実行中プロセスを確認し、stale lock なら安全に取り除いてください",
        )
    preserved = _preserve_worktree(root, output)
    old_state = _read_salvageable_state(root)
    recovered = derive_recovered_state(root, old_state)
    path = root / ".story-pipeline/state.json"
    atomic_write_json(path, recovered)
    commit = commit_explicit_paths(
        root,
        (".story-pipeline/state.json",),
        "Recover story state",
        body=("Abandoned active request and pending work",),
    )
    if commit is None:
        raise _error("状態復旧の commit 対象がありません", str(path))
    if preserved is not None:
        print(f"Preservation commit: {preserved}", file=output)
    print(f"Recovered state: {recovered['phase']}", file=output)
    print(f"Commit: {commit}", file=output)
    return 0


def derive_recovered_state(root: Path, old_state: dict[str, Any]) -> dict[str, Any]:
    """安全に確認できる成果物と旧 state の一部だけから正規 state を返す。"""
    base = {
        "schema_version": 1,
        "phase": "concept",
        "next_chapter": 1,
        "next_episode": 1,
        "completed_chapters": [],
        "completed_episodes": [],
        "current_chapter": None,
        "pending_reviews": [],
        "pending_decisions": [],
        "last_request": _last_run_number(root),
        "active_request": None,
        "updated_at": utc_timestamp(),
    }
    if not _safe_file(root / "concept.md"):
        return validate_state_data(base)
    if not all(_safe_file(root / name) for name in FOUNDATION_FILES):
        base["phase"] = "foundation"
        return validate_state_data(base)
    if not _safe_file(root / "plot.md"):
        base["phase"] = "plotting"
        return validate_state_data(base)
    if not _has_numbered_files(root / "chapters", ".md"):
        base["phase"] = "plotting"
        return validate_state_data(base)

    structure = load_story_structure(root)
    episodes = _actual_episode_prefix(root, structure)
    chapters = _salvage_completed_chapters(old_state, structure, set(episodes))
    base["completed_episodes"] = list(episodes)
    base["completed_chapters"] = list(chapters)
    remaining = [chapter for chapter in structure.chapters if chapter.number not in chapters]
    if not remaining:
        base.update({
            "phase": "final_revision",
            "next_chapter": _sentinel(structure.chapter_numbers),
            "next_episode": _sentinel(structure.episode_numbers),
        })
        return validate_state_data(base)
    current = remaining[0]
    missing = [number for number in current.episodes if number not in episodes]
    base["current_chapter"] = current.number
    base["next_chapter"] = current.number
    if missing:
        next_episode = missing[0]
        base["next_episode"] = next_episode
        base["phase"] = (
            "drafting" if _safe_file(root / f"episode_plans/{next_episode:04d}.md")
            else "episode_planning"
        )
    else:
        later = [
            number for chapter in remaining[1:] for number in chapter.episodes
            if number not in episodes
        ]
        base["phase"] = "chapter_revision"
        base["next_episode"] = later[0] if later else _sentinel(structure.episode_numbers)
    return validate_state_data(base)


def _preserve_worktree(root: Path, output: TextIO) -> str | None:
    entries = read_worktree(root)
    paths: set[str] = set()
    for entry in entries:
        path = entry.normalized_path()
        if entry.kind == "ignored":
            continue
        if entry.kind == "unmerged":
            raise _error("競合を解消する必要があります", path)
        if path in TEMPORARY:
            if entry.index_status not in {".", "?"} or entry.kind == "tracked":
                raise _error("秘密または一時ファイルは保全 commit に含められません", path)
            continue
        if not entry.rename_origin:
            paths.add(path)
    if not paths:
        return None
    for path in sorted(paths):
        print(f"Preserving worktree file: {path}", file=output)
    return commit_explicit_paths(root, tuple(sorted(paths)), "Preserve worktree before recovery")


def _read_salvageable_state(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / ".story-pipeline/state.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _salvage_completed_chapters(
    state: dict[str, Any], structure: StoryStructure, episodes: set[int]
) -> tuple[int, ...]:
    value = state.get("completed_chapters")
    if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return ()
    wanted = tuple(value)
    prefix = structure.chapter_numbers[:len(wanted)]
    if wanted != prefix:
        return ()
    valid: list[int] = []
    for number in wanted:
        if not set(structure.chapter(number).episodes) <= episodes:
            break
        valid.append(number)
    return tuple(valid)


def _actual_episode_prefix(root: Path, structure: StoryStructure) -> tuple[int, ...]:
    actual = tuple(number for number in structure.episode_numbers if _safe_file(root / f"episodes/{number:04d}.md"))
    if actual != structure.episode_numbers[:len(actual)]:
        raise _error("本文ファイルが章・話対応表の連続した prefix ではありません", "episodes")
    known = {f"{number:04d}.md" for number in structure.episode_numbers}
    try:
        extras = sorted(
            item.name for item in (root / "episodes").iterdir()
            if re.fullmatch(r"[0-9]{4}\.md", item.name) and item.name not in known
        )
    except OSError as error:
        raise _error("本文ディレクトリを読み取れません", "episodes", code=4) from error
    if extras:
        raise _error("章・話対応表にない本文があります", f"episodes/{extras[0]}", code=4)
    return actual


def _last_run_number(root: Path) -> int | None:
    directory = root / ".story-pipeline/runs"
    try:
        numbers = [int(match.group(1)) for item in directory.iterdir() if (match := NUMBERED_JSON.fullmatch(item.name))]
    except OSError:
        return None
    return max(numbers) if numbers else None


def _has_numbered_files(directory: Path, suffix: str) -> bool:
    try:
        return any(re.fullmatch(rf"[0-9]{{4}}{re.escape(suffix)}", item.name) for item in directory.iterdir())
    except OSError:
        return False


def _safe_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _sentinel(numbers: tuple[int, ...]) -> int:
    return numbers[-1] if numbers[-1] == 9999 else numbers[-1] + 1


def _error(reason: str, location: str, action: str = "Git と作品ファイルを確認してください", code: int = 5) -> StoryPipelineError:
    return StoryPipelineError(reason, location, action, code)
