"""作品状態の副作用のない軽量照合。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from story_pipeline.errors import StoryPipelineError
from story_pipeline.story_structure import StoryStructure, load_story_structure


NUMBERED_MARKDOWN = re.compile(r"^([0-9]{4})\.md$")
PHASE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "foundation": ("concept.md",),
    "plotting": ("concept.md", "world.md", "characters.md", "style.md", "canon.md"),
    "episode_planning": ("concept.md", "world.md", "characters.md", "style.md", "canon.md", "plot.md"),
    "drafting": ("concept.md", "world.md", "characters.md", "style.md", "canon.md", "plot.md"),
    "chapter_revision": ("concept.md", "world.md", "characters.md", "style.md", "canon.md", "plot.md"),
    "final_revision": ("concept.md", "world.md", "characters.md", "style.md", "canon.md", "plot.md"),
    "completed": ("concept.md", "world.md", "characters.md", "style.md", "canon.md", "plot.md"),
}


@dataclass(frozen=True, slots=True)
class StatusWarning:
    code: str
    message: str
    location: str = ""


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    root: Path
    state: dict[str, Any]
    last_request_status: str | None
    lock_info: str | None
    warnings: tuple[StatusWarning, ...]


def determine_next_action(root: Path, state: dict[str, Any]) -> str:
    """状態から、仕様上の次の自然な作業単位を決定する。"""
    if state["pending_decisions"]:
        return f"answer decision {state['pending_decisions'][0]['id']}"
    if state["active_request"] is not None:
        return f"resume request {state['active_request']:04d}"
    if state["pending_reviews"]:
        review = state["pending_reviews"][0]
        target = review["target_type"]
        number = review["target_number"]
        suffix = "" if number is None else f" {number:04d}"
        return f"review {target}{suffix}"

    phase = state["phase"]
    episode = state["next_episode"]
    chapter = state["current_chapter"] or state["next_chapter"]
    if phase == "concept":
        return "create concept"
    if phase == "foundation":
        return "create foundation files"
    if phase == "plotting":
        return "create plot and chapter plan"
    if phase == "episode_planning":
        return f"create episode plan {episode:04d}"
    if phase == "drafting":
        plan = Path("episode_plans") / f"{episode:04d}.md"
        if not _safe_regular_file(root, plan):
            return f"create episode plan {episode:04d}"
        return f"draft episode {episode:04d}"
    if phase == "chapter_revision":
        return f"review chapter {chapter:04d}"
    if phase == "final_revision":
        return "review complete novel"
    return "report completed project"


def inspect_status(root: Path, state: dict[str, Any]) -> StatusSnapshot:
    """状態と主要ファイルを軽く照合し、ファイルを変更せず結果を返す。"""
    warnings: list[StatusWarning] = []
    _check_completed_files(root, state, "chapters", "completed_chapters", warnings)
    _check_completed_files(root, state, "episodes", "completed_episodes", warnings)
    _check_next_number(root, state, "chapters", "next_chapter", warnings)
    _check_next_number(root, state, "episodes", "next_episode", warnings)
    _check_phase_artifacts(root, state["phase"], warnings)
    _check_story_structure(root, state, warnings)
    _check_request_files(root, state, warnings)
    last_status = _read_last_request_status(root, state["last_request"], warnings)
    lock_info = _read_lock(root, warnings)
    return StatusSnapshot(root, state, last_status, lock_info, tuple(warnings))


def _check_story_structure(
    root: Path, state: dict[str, Any], warnings: list[StatusWarning]
) -> None:
    directory = root / "chapters"
    try:
        has_chapters = any(NUMBERED_MARKDOWN.fullmatch(item.name) for item in directory.iterdir())
    except OSError:
        return
    if not has_chapters:
        return
    try:
        structure = load_story_structure(root)
    except StoryPipelineError as error:
        warnings.append(StatusWarning("STORY_STRUCTURE_INVALID", error.reason, error.location))
        return
    chapter_numbers = structure.chapter_numbers
    episode_numbers = structure.episode_numbers
    completed_chapters = tuple(state["completed_chapters"])
    completed_episodes = tuple(state["completed_episodes"])
    if completed_chapters != chapter_numbers[:len(completed_chapters)]:
        warnings.append(StatusWarning(
            "COMPLETED_CHAPTER_SEQUENCE",
            "completed_chapters が章対応表の完了済み prefix と一致しません。",
            "/completed_chapters",
        ))
    if completed_episodes != episode_numbers[:len(completed_episodes)]:
        warnings.append(StatusWarning(
            "COMPLETED_EPISODE_SEQUENCE",
            "completed_episodes が話対応表の完了済み prefix と一致しません。",
            "/completed_episodes",
        ))
    for chapter_number in completed_chapters:
        chapter = structure.chapter(chapter_number)
        if not set(chapter.episodes) <= set(completed_episodes):
            warnings.append(StatusWarning(
                "COMPLETED_CHAPTER_EPISODES",
                "完了章に未完了の収録話があります。",
                f"{chapter.path} ## 収録話",
            ))
    _check_phase_transition_state(structure, state, warnings)


def _check_phase_transition_state(
    structure: StoryStructure,
    state: dict[str, Any],
    warnings: list[StatusWarning],
) -> None:
    phase = state["phase"]
    if phase not in {"episode_planning", "drafting", "chapter_revision", "final_revision", "completed"}:
        return
    completed_chapters = set(state["completed_chapters"])
    completed_episodes = set(state["completed_episodes"])
    remaining_chapters = [item for item in structure.chapters if item.number not in completed_chapters]
    remaining_episodes = [item for item in structure.episode_numbers if item not in completed_episodes]
    if phase in {"final_revision", "completed"}:
        if remaining_chapters:
            warnings.append(StatusWarning(
                "FINAL_PHASE_INCOMPLETE_CHAPTERS",
                f"phase={phase} ですが未完了章があります。",
                "/phase",
            ))
        if state["current_chapter"] is not None:
            warnings.append(StatusWarning(
                "FINAL_PHASE_CURRENT_CHAPTER", "全体段階では current_chapter は null が必要です。",
                "/current_chapter",
            ))
        return
    current = state["current_chapter"]
    if current is None:
        warnings.append(StatusWarning(
            "CURRENT_CHAPTER_REQUIRED", f"phase={phase} には current_chapter が必要です。",
            "/current_chapter",
        ))
        return
    try:
        chapter = structure.chapter(current)
    except StoryPipelineError:
        warnings.append(StatusWarning(
            "CURRENT_CHAPTER_UNKNOWN", "current_chapter が章対応表にありません。", "/current_chapter"
        ))
        return
    if current in completed_chapters:
        warnings.append(StatusWarning(
            "CURRENT_CHAPTER_COMPLETED", "current_chapter はすでに完了しています。", "/current_chapter"
        ))
    missing_in_current = [item for item in chapter.episodes if item not in completed_episodes]
    if phase == "chapter_revision":
        if missing_in_current:
            warnings.append(StatusWarning(
                "CHAPTER_REVISION_EPISODES_INCOMPLETE",
                "章改稿対象に未完了の収録話があります。",
                f"{chapter.path} ## 収録話",
            ))
        expected_episode = remaining_episodes[0] if remaining_episodes else _sentinel(structure.episode_numbers)
    else:
        if not missing_in_current:
            warnings.append(StatusWarning(
                "CHAPTER_REVISION_REQUIRED",
                "章内全話が完了しているため chapter_revision が必要です。",
                "/phase",
            ))
            return
        expected_episode = missing_in_current[0]
    if state["next_episode"] != expected_episode:
        warnings.append(StatusWarning(
            "NEXT_EPISODE_MISMATCH",
            f"next_episode={state['next_episode']:04d} は期待値 {expected_episode:04d} と一致しません。",
            "/next_episode",
        ))
    if state["next_chapter"] != current:
        warnings.append(StatusWarning(
            "NEXT_CHAPTER_MISMATCH",
            "制作中は next_chapter と current_chapter が一致する必要があります。",
            "/next_chapter",
        ))


def _sentinel(numbers: tuple[int, ...]) -> int:
    return numbers[-1] if numbers[-1] == 9999 else numbers[-1] + 1


def _check_completed_files(
    root: Path,
    state: dict[str, Any],
    directory_name: str,
    state_key: str,
    warnings: list[StatusWarning],
) -> None:
    for number in state[state_key]:
        relative = Path(directory_name) / f"{number:04d}.md"
        if not _safe_regular_file(root, relative):
            warnings.append(StatusWarning(
                "COMPLETED_FILE_MISSING",
                f"完了済み番号に対応する通常ファイルがありません: {relative.as_posix()}",
            ))


def _check_next_number(
    root: Path,
    state: dict[str, Any],
    directory_name: str,
    state_key: str,
    warnings: list[StatusWarning],
) -> None:
    numbers = _numbered_files(root, Path(directory_name))
    if (
        directory_name == "chapters"
        and state["current_chapter"] == state[state_key]
        and state["phase"] not in {"final_revision", "completed"}
    ):
        return
    if (
        numbers
        and max(numbers) == 9999
        and state[state_key] == 9999
        and state["phase"] in {"chapter_revision", "final_revision", "completed"}
    ):
        return
    if numbers and state[state_key] <= max(numbers):
        warnings.append(StatusWarning(
            "NEXT_NUMBER_CONFLICT",
            f"{state_key}={state[state_key]} が既存最大番号 {max(numbers):04d} 以下です。",
        ))


def _check_phase_artifacts(root: Path, phase: str, warnings: list[StatusWarning]) -> None:
    for relative_text in PHASE_ARTIFACTS.get(phase, ()):
        relative = Path(relative_text)
        if not _safe_regular_file(root, relative):
            warnings.append(StatusWarning(
                "PHASE_ARTIFACT_MISSING",
                f"phase={phase} に必要な通常ファイルがありません: {relative_text}",
            ))


def _check_request_files(
    root: Path, state: dict[str, Any], warnings: list[StatusWarning]
) -> None:
    for key in ("last_request", "active_request"):
        number = state[key]
        if number is None:
            continue
        relative = Path("requests") / f"{number:04d}.md"
        if not _safe_regular_file(root, relative):
            warnings.append(StatusWarning(
                "REQUEST_FILE_MISSING",
                f"{key} に対応する要求ファイルがありません: {relative.as_posix()}",
            ))
    last_request = state["last_request"]
    if last_request is not None and state["active_request"] is None:
        report = Path("requests") / f"{last_request:04d}_agent.md"
        if not _safe_regular_file(root, report):
            warnings.append(StatusWarning(
                "REPORT_FILE_MISSING",
                f"終了済み要求の処理報告がありません: {report.as_posix()}",
            ))


def _read_last_request_status(
    root: Path, number: int | None, warnings: list[StatusWarning]
) -> str | None:
    if number is None:
        return None
    relative = Path(".story-pipeline") / "runs" / f"{number:04d}.json"
    path = root / relative
    if not _safe_regular_file(root, relative):
        warnings.append(StatusWarning(
            "RUN_FILE_MISSING",
            f"last_request に対応する実行記録がありません: {relative.as_posix()}",
        ))
        return "unknown"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        status = value["status"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        warnings.append(StatusWarning(
            "RUN_STATUS_INVALID", f"実行記録の status を読み取れません: {relative.as_posix()}"
        ))
        return "unknown"
    if status not in {"running", "completed", "failed", "awaiting_human"}:
        warnings.append(StatusWarning(
            "RUN_STATUS_INVALID", f"実行記録の status が不正です: {relative.as_posix()}"
        ))
        return "unknown"
    return status


def _read_lock(root: Path, warnings: list[StatusWarning]) -> str | None:
    relative = Path(".story-pipeline") / "run.lock"
    path = root / relative
    if not path.exists() and not path.is_symlink():
        return None
    if not _safe_regular_file(root, relative):
        warnings.append(StatusWarning("LOCK_INVALID", "run.lock が安全な通常ファイルではありません。"))
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pid = value["pid"]
        hostname = value["hostname"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        warnings.append(StatusWarning("LOCK_INVALID", "run.lock のプロセス情報を読み取れません。"))
        return None
    return f"pid={pid}, hostname={hostname}"


def _numbered_files(root: Path, relative_directory: Path) -> list[int]:
    directory = root / relative_directory
    try:
        target = directory.resolve(strict=True)
        target.relative_to(root)
        if not target.is_dir():
            return []
        entries = directory.iterdir()
    except (OSError, ValueError):
        return []
    numbers: list[int] = []
    for entry in entries:
        match = NUMBERED_MARKDOWN.fullmatch(entry.name)
        relative = relative_directory / entry.name
        if match and _safe_regular_file(root, relative):
            numbers.append(int(match.group(1)))
    return numbers


def _safe_regular_file(root: Path, relative: Path) -> bool:
    path = root / relative
    try:
        target = path.resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError):
        return False
    return target.is_file()
