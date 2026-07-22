"""既存の誤った制作状態を明示操作で安全に移行する。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import stat
from typing import Any, TextIO

from story_pipeline.config import load_config
from story_pipeline.errors import StoryPipelineError
from story_pipeline.git_safety import commit_explicit_paths, inspect_run_preconditions
from story_pipeline.persistence import atomic_write_json
from story_pipeline.request_selection import has_meaningful_request_content
from story_pipeline.run_lifecycle import utc_timestamp
from story_pipeline.state import load_state, validate_state_data
from story_pipeline.story_structure import StoryStructure, load_story_structure


def migrate_state_command(root: Path, output: TextIO) -> int:
    """Git と成果物を検査し、必要な state 遷移だけを commit する。"""
    config = load_config(root)
    state = load_state(root)
    if state["active_request"] is not None:
        raise _migration_error("active request の実行中は状態を移行できません", "/active_request")
    preflight = inspect_run_preconditions(root, config)
    _require_migration_worktree(root, preflight.entries)
    structure = load_story_structure(root)
    updates = derive_migrated_state(root, structure, state)
    if all(state[key] == value for key, value in updates.items()):
        print("State migration is not required.", file=output)
        return 0
    migrated = deepcopy(state)
    migrated.update(updates)
    migrated["updated_at"] = utc_timestamp()
    validate_state_data(migrated)
    path = root / ".story-pipeline/state.json"
    atomic_write_json(path, migrated)
    commit = commit_explicit_paths(
        root,
        (".story-pipeline/state.json",),
        "Migrate story state",
        body=(f"Phase: {state['phase']} -> {migrated['phase']}",),
    )
    if commit is None:
        raise _migration_error("状態 migration の commit 対象がありません", str(path))
    print(f"Migrated state: {state['phase']} -> {migrated['phase']}", file=output)
    print(f"Commit: {commit}", file=output)
    return 0


def derive_migrated_state(
    root: Path, structure: StoryStructure, state: dict[str, Any]
) -> dict[str, Any]:
    """作品ファイルと既存完了集合が一致するときの正規遷移状態を返す。"""
    actual_episodes = tuple(
        number for number in structure.episode_numbers
        if _safe_file(root / f"episodes/{number:04d}.md")
    )
    if actual_episodes != structure.episode_numbers[:len(actual_episodes)]:
        raise _migration_error("本文ファイルが対応表の連続した prefix ではありません", "episodes")
    if tuple(state["completed_episodes"]) != actual_episodes:
        raise _migration_error(
            "completed_episodes と実在本文が一致しないため自動移行できません",
            "/completed_episodes",
        )
    completed_chapters = tuple(state["completed_chapters"])
    if completed_chapters != structure.chapter_numbers[:len(completed_chapters)]:
        raise _migration_error(
            "completed_chapters が章対応表の prefix ではありません", "/completed_chapters"
        )
    completed_episode_set = set(actual_episodes)
    for number in completed_chapters:
        if not set(structure.chapter(number).episodes) <= completed_episode_set:
            raise _migration_error(
                "完了章に実在しない収録話があります", f"chapters/{number:04d}.md ## 収録話"
            )
    remaining = [item for item in structure.chapters if item.number not in completed_chapters]
    if not remaining:
        return {
            "phase": "final_revision",
            "current_chapter": None,
            "next_chapter": _sentinel(structure.chapter_numbers),
            "next_episode": _sentinel(structure.episode_numbers),
        }
    current = remaining[0]
    missing = [number for number in current.episodes if number not in completed_episode_set]
    if missing:
        return {
            "phase": "episode_planning",
            "current_chapter": current.number,
            "next_chapter": current.number,
            "next_episode": missing[0],
        }
    later_episodes = [
        number for item in remaining[1:] for number in item.episodes
        if number not in completed_episode_set
    ]
    return {
        "phase": "chapter_revision",
        "current_chapter": current.number,
        "next_chapter": current.number,
        "next_episode": later_episodes[0] if later_episodes else _sentinel(structure.episode_numbers),
    }


def _require_migration_worktree(root: Path, entries: tuple[Any, ...]) -> None:
    for entry in entries:
        if entry.kind == "ignored":
            continue
        relative = entry.normalized_path()
        if entry.kind == "untracked" and relative.startswith("requests/"):
            path = root / relative
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise _migration_error("未追跡要求を安全に確認できません", relative) from error
            if not has_meaningful_request_content(content):
                continue
        raise _migration_error("状態 migration 前に Git 作業ツリーを整理する必要があります", relative)


def _safe_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _sentinel(numbers: tuple[int, ...]) -> int:
    return numbers[-1] if numbers[-1] == 9999 else numbers[-1] + 1


def _migration_error(reason: str, location: str) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        location,
        "Git、章・話対応表、state、本文ファイルを確認してから再実行してください",
        5,
    )
