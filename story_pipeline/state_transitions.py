"""検証済み章・話対応表に基づく制作状態遷移。"""

from __future__ import annotations

from typing import Any

from story_pipeline.errors import StoryPipelineError
from story_pipeline.story_structure import StoryStructure


def transition_after_draft(
    structure: StoryStructure,
    state: dict[str, Any],
    episode_number: int,
) -> dict[str, Any]:
    """本文採用後、章内の次話または章改稿へ進める。"""
    chapter = structure.chapter_for_episode(episode_number)
    current = state.get("current_chapter") or chapter.number
    if current != chapter.number:
        raise _transition_error(
            "採用された本文が、現在制作中の章に含まれる話ではありません",
            "/current_chapter",
        )
    completed = sorted(set((*state.get("completed_episodes", []), episode_number)))
    _require_subset(completed, structure.episode_numbers, "/completed_episodes")
    missing_in_chapter = [number for number in chapter.episodes if number not in completed]
    if missing_in_chapter:
        return {
            "phase": "episode_planning",
            "current_chapter": chapter.number,
            "next_chapter": chapter.number,
            "next_episode": min(missing_in_chapter),
            "completed_episodes": completed,
        }
    remaining = [number for number in structure.episode_numbers if number not in completed]
    return {
        "phase": "chapter_revision",
        "current_chapter": chapter.number,
        "next_chapter": chapter.number,
        "next_episode": min(remaining) if remaining else _sentinel(structure.episode_numbers),
        "completed_episodes": completed,
    }


def transition_after_chapter(
    structure: StoryStructure,
    state: dict[str, Any],
    chapter_number: int,
) -> dict[str, Any]:
    """章改稿完了後、次章の最初の話または全体改稿へ進める。"""
    structure.chapter(chapter_number)
    completed = sorted(set((*state.get("completed_chapters", []), chapter_number)))
    _require_subset(completed, structure.chapter_numbers, "/completed_chapters")
    remaining = [item for item in structure.chapters if item.number not in completed]
    if remaining:
        chapter = remaining[0]
        completed_episodes = set(state.get("completed_episodes", []))
        missing = [number for number in chapter.episodes if number not in completed_episodes]
        if not missing:
            raise _transition_error(
                "未完了の章に未制作の話がありません。章計画の収録話と制作状態が矛盾しています",
                f"{chapter.path} ## 収録話",
            )
        return {
            "phase": "episode_planning",
            "completed_chapters": completed,
            "current_chapter": chapter.number,
            "next_chapter": chapter.number,
            "next_episode": min(missing),
        }
    return {
        "phase": "final_revision",
        "completed_chapters": completed,
        "current_chapter": None,
        "next_chapter": _sentinel(structure.chapter_numbers),
        "next_episode": _sentinel(structure.episode_numbers),
    }


def all_chapters_complete_after(
    structure: StoryStructure, completed_chapters: list[int] | tuple[int, ...], chapter_number: int
) -> bool:
    completed = set((*completed_chapters, chapter_number))
    return completed == set(structure.chapter_numbers)


def _sentinel(numbers: tuple[int, ...]) -> int:
    maximum = max(numbers)
    return maximum if maximum == 9999 else maximum + 1


def _require_subset(values: list[int], allowed: tuple[int, ...], location: str) -> None:
    unknown = set(values) - set(allowed)
    if unknown:
        raise _transition_error(
            f"対応表にない完了番号があります: {min(unknown):04d}", location
        )


def _transition_error(reason: str, location: str) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        location,
        "状態と章・話対応表を validate で確認してください",
        4,
    )
