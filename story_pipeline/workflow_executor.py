"""制作フェーズ別 workflow を共通の採用結果へ変換する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from story_pipeline.chapter_revision_workflow import produce_chapter_revision
from story_pipeline.concept_workflow import produce_concept
from story_pipeline.drafting_workflow import produce_draft
from story_pipeline.episode_planning_workflow import produce_episode_plan
from story_pipeline.final_revision_workflow import produce_final_revision
from story_pipeline.foundation_workflow import produce_foundation
from story_pipeline.llm_client import CompletionResult, LLMClient
from story_pipeline.plotting_workflow import produce_plotting
from story_pipeline.request_planner import PlannedRequest


@dataclass(frozen=True, slots=True)
class ExecutedCall:
    role: str
    purpose: str
    completion: CompletionResult


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    status: str
    phase: str
    documents: tuple[tuple[str, str], ...]
    state_updates: dict[str, Any]
    calls: tuple[ExecutedCall, ...]
    evaluation: str | None
    reason: str | None
    internal_files: tuple[str, ...] = ()
    diagnostics: tuple[Any, ...] = ()


def execute_planned_workflow(
    root: Any,
    state: dict[str, Any],
    planned: PlannedRequest,
    client: LLMClient,
    *,
    request_revision: int = 0,
) -> WorkflowExecution:
    """決定済み scope を実行し、採用可能な文書だけを返す。"""
    phase = planned.scope.phase
    request = planned.request
    interpretation = planned.interpretation
    if phase == "concept":
        result = produce_concept(root, request, interpretation, client)
        documents = () if result.best is None else (("concept.md", result.best.candidate.content),)
        updates = {"phase": "foundation"} if documents else {}
    elif phase == "foundation":
        result = produce_foundation(root, request, interpretation, client)
        documents = () if result.best is None else result.best.candidate.documents
        updates = {"phase": "plotting"} if documents else {}
    elif phase == "plotting":
        result = produce_plotting(root, request, interpretation, client)
        documents = () if result.best is None else result.best.candidate.documents
        updates = (
            {"phase": "episode_planning", "current_chapter": state["current_chapter"] or 1}
            if documents
            else {}
        )
    elif phase == "episode_planning":
        number = _target_number(planned.scope.targets[0], state["next_episode"])
        result = produce_episode_plan(root, request, interpretation, number, client)
        documents = (
            ()
            if result.best is None
            else ((result.best.candidate.path, result.best.candidate.content),)
        )
        updates = {"phase": "drafting"} if documents else {}
    elif phase == "drafting":
        number = _target_number(planned.scope.targets[0], state["next_episode"])
        result = produce_draft(
            root, request, interpretation, number, client,
            request_revision=request_revision,
        )
        documents = (
            ()
            if result.best is None
            else ((result.best.candidate.path, result.best.candidate.content),)
        )
        completed = sorted(set((*state["completed_episodes"], number)))
        updates = (
            {
                "phase": "episode_planning",
                "completed_episodes": completed,
                "next_episode": min(9999, max(state["next_episode"], number + 1)),
            }
            if documents
            else {}
        )
    elif phase == "chapter_revision":
        number = _target_number(planned.scope.targets[0], state["current_chapter"] or 1)
        result = produce_chapter_revision(
            root,
            request,
            interpretation,
            number,
            client,
            completed_chapters=tuple(state["completed_chapters"]),
        )
        documents = () if result.best is None else result.best.documents
        updates = {}
        if result.completion_update is not None:
            completion = result.completion_update
            documents = (*documents, (completion.chapter_path, completion.chapter_content))
            updates = {
                "phase": completion.next_phase,
                "completed_chapters": list(completion.completed_chapters),
                "next_chapter": completion.next_chapter,
                "current_chapter": None if completion.next_phase == "final_revision" else completion.next_chapter,
            }
    elif phase in {"final_revision", "completed"}:
        if phase == "completed" or planned.scope.action == "report_completed":
            return WorkflowExecution("completed", phase, (), {}, (), "完成済み", None)
        result = produce_final_revision(
            root,
            request,
            interpretation,
            client,
            completed_chapters=tuple(state["completed_chapters"]),
            completed_episodes=tuple(state["completed_episodes"]),
            pending_reviews=tuple(state["pending_reviews"]),
            pending_decisions=tuple(state["pending_decisions"]),
        )
        documents = () if result.best is None else result.best.documents
        updates = {}
        if result.completion_update is not None:
            completion = result.completion_update
            updates = {
                "phase": completion.phase,
                "completed_chapters": list(completion.completed_chapters),
                "completed_episodes": list(completion.completed_episodes),
                "current_chapter": completion.current_chapter,
                "pending_reviews": list(completion.pending_reviews),
                "pending_decisions": list(completion.pending_decisions),
            }
    else:
        raise ValueError(f"未対応の制作フェーズです: {phase}")

    calls = tuple(ExecutedCall(item.role, item.purpose, item.completion) for item in result.calls)
    evaluation = _evaluation_summary(result)
    internal_files = (
        (result.checkpoint_path,)
        if phase == "drafting" and result.checkpoint_path is not None
        else ()
    )
    diagnostics = tuple(getattr(result, "diagnostics", ()))
    return WorkflowExecution(
        result.status, phase, tuple(dict(documents).items()), updates, calls, evaluation,
        result.reason, internal_files, diagnostics,
    )


def _target_number(path: str, fallback: int) -> int:
    try:
        value = int(path.rsplit("/", 1)[-1].removesuffix(".md"))
    except ValueError:
        return fallback
    return value if 1 <= value <= 9999 else fallback


def _evaluation_summary(result: Any) -> str | None:
    if result.best is not None:
        evaluation = result.best.evaluation
        return getattr(evaluation, "summary", None) or getattr(evaluation, "reason", None)
    if result.candidates:
        evaluation = result.candidates[-1].evaluation
        return getattr(evaluation, "summary", None) or getattr(evaluation, "reason", None)
    return result.reason
