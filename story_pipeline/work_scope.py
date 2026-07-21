"""要求解釈と作品状態から今回の有限な制作単位を決定する。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from story_pipeline.errors import StoryPipelineError
from story_pipeline.git_validation import classify_path, normalize_git_path
from story_pipeline.request_interpretation import RequestInterpretation


FOUNDATION_FILES = ("world.md", "characters.md", "style.md", "canon.md")


@dataclass(frozen=True, slots=True)
class WorkScope:
    phase: str
    action: str
    targets: tuple[str, ...]
    units: int
    requested_until: str | None


def determine_work_scope(
    root: Path, state: dict[str, Any], interpretation: RequestInterpretation
) -> WorkScope:
    """明示対象を優先し、継続要求では固定された最初の規則を選ぶ。"""
    if interpretation.ambiguities:
        raise _scope_error(f"要求解釈に未解決の曖昧さがあります: {interpretation.ambiguities[0]}")
    if state["pending_decisions"]:
        return WorkScope(
            state["phase"],
            "resolve_decisions",
            tuple(item["id"] for item in state["pending_decisions"]),
            1,
            None,
        )
    if interpretation.kind in {"modify", "add", "reconsider"} or (
        interpretation.kind == "mixed" and interpretation.targets
    ):
        return _explicit_scope(interpretation)
    return _standard_scope(root, state, interpretation)


def _standard_scope(
    root: Path, state: dict[str, Any], interpretation: RequestInterpretation
) -> WorkScope:
    if not _safe_file(root, "concept.md"):
        return WorkScope("concept", "create_concept", ("concept.md",), 1, interpretation.requested_until)
    missing_foundation = tuple(path for path in FOUNDATION_FILES if not _safe_file(root, path))
    if missing_foundation:
        return WorkScope("foundation", "create_foundation", FOUNDATION_FILES, 1, interpretation.requested_until)

    chapter_number = state["current_chapter"] or state["next_chapter"]
    chapter_path = f"chapters/{chapter_number:04d}.md"
    if not _safe_file(root, "plot.md") or not _safe_file(root, chapter_path):
        return WorkScope("plotting", "create_plot", ("plot.md", chapter_path), 1, interpretation.requested_until)

    if state["pending_reviews"]:
        review = state["pending_reviews"][0]
        target_type = review["target_type"]
        number = review["target_number"]
        if target_type == "episode":
            target = f"episodes/{number:04d}.md"
            phase = "drafting"
        elif target_type == "chapter":
            target = f"chapters/{number:04d}.md"
            phase = "chapter_revision"
        else:
            target = "novel"
            phase = "final_revision"
        return WorkScope(phase, f"review_{target_type}", (target,), 1, interpretation.requested_until)

    episode_number = state["next_episode"]
    plan = f"episode_plans/{episode_number:04d}.md"
    episode = f"episodes/{episode_number:04d}.md"
    if not _safe_file(root, plan):
        return WorkScope(
            "episode_planning",
            "create_episode_plan",
            (plan,),
            interpretation.requested_units,
            interpretation.requested_until,
        )
    if not _safe_file(root, episode):
        return WorkScope(
            "drafting",
            "draft_episode",
            (episode,),
            interpretation.requested_units,
            interpretation.requested_until,
        )
    if state["phase"] == "chapter_revision":
        return WorkScope("chapter_revision", "review_chapter", (chapter_path,), 1, interpretation.requested_until)
    if state["phase"] in {"final_revision", "completed"}:
        action = "report_completed" if state["phase"] == "completed" else "review_novel"
        return WorkScope(state["phase"], action, ("novel",), 1, interpretation.requested_until)
    raise _scope_error("状態と実ファイルから次の制作単位を一意に決定できません")


def _explicit_scope(interpretation: RequestInterpretation) -> WorkScope:
    if not interpretation.targets:
        raise _scope_error("変更・追加要求に明示的な対象がありません")
    phases: set[str] = set()
    targets: list[str] = []
    for target in interpretation.targets:
        normalized = normalize_git_path(target)
        if normalized is None or classify_path(normalized) != "managed":
            raise _scope_error(f"管理対象ファイルとして扱えない対象です: {target}")
        phases.add(_phase_for_target(normalized))
        targets.append(normalized)
    phase = phases.pop() if len(phases) == 1 else "mixed"
    return WorkScope(
        phase,
        f"{interpretation.kind}_targets",
        tuple(targets),
        interpretation.requested_units,
        interpretation.requested_until,
    )


def _phase_for_target(path: str) -> str:
    if path == "concept.md":
        return "concept"
    if path in FOUNDATION_FILES:
        return "foundation"
    if path == "plot.md" or path.startswith("chapters/"):
        return "plotting"
    if path.startswith("episode_plans/"):
        return "episode_planning"
    if path.startswith("episodes/"):
        return "drafting"
    if path == ".story-pipeline/state.json" or path.startswith("requests/"):
        raise _scope_error(f"要求から直接変更できない管理対象です: {path}")
    return "foundation"


def _safe_file(root: Path, relative: str) -> bool:
    path = root / PurePosixPath(relative)
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _scope_error(reason: str) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        "work scope",
        "対象と変更意思を要求へ明記してください",
        8,
    )
