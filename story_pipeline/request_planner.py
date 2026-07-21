"""要求選択後の planner 呼び出しと作業範囲決定を接続する。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from story_pipeline.context_builder import build_interpretation_messages, interpretation_response_format
from story_pipeline.decision_resolution import DecisionResolution, resolve_pending_decisions
from story_pipeline.llm_client import CompletionResult, LLMClient
from story_pipeline.request_interpretation import RequestInterpretation, parse_request_interpretation
from story_pipeline.request_selection import SelectedRequest
from story_pipeline.work_scope import WorkScope, determine_work_scope


@dataclass(frozen=True, slots=True)
class PlannedRequest:
    request: SelectedRequest
    interpretation: RequestInterpretation
    decisions: tuple[DecisionResolution, ...]
    scope: WorkScope
    completion: CompletionResult


def plan_selected_request(
    root: Path,
    state: dict[str, Any],
    request: SelectedRequest,
    client: LLMClient,
    *,
    context_paths: list[str] | tuple[str, ...] = (),
) -> PlannedRequest:
    """planner 応答を検証し、未検証の値をスコープへ渡さない。"""
    messages = build_interpretation_messages(root, request, context_paths)
    completion = client.complete_role(
        "planner", messages, response_format=interpretation_response_format()
    )
    interpretation = parse_request_interpretation(completion.response.content, request.content)
    decisions = resolve_pending_decisions(state, interpretation)
    scope = determine_work_scope(root, state, interpretation)
    return PlannedRequest(request, interpretation, decisions, scope, completion)
