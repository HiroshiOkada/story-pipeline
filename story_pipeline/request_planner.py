"""要求選択後の planner 呼び出しと作業範囲決定を接続する。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from story_pipeline.context_builder import build_interpretation_messages, interpretation_response_format
from story_pipeline.decision_resolution import DecisionResolution, resolve_pending_decisions
from story_pipeline.errors import StoryPipelineError
from story_pipeline.llm_client import CompletionResult, LLMClient
from story_pipeline.llm_transport import ApiFailure
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
    logical_calls: int


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
    maximum_calls = client.config["limits"]["generation_calls"]
    completion: CompletionResult | None = None
    interpretation: RequestInterpretation | None = None
    for logical_call in range(1, maximum_calls + 1):
        try:
            completion = client.complete_role(
                "planner", messages, response_format=interpretation_response_format()
            )
        except ApiFailure as error:
            if error.kind != "invalid_response" or logical_call >= maximum_calls:
                raise
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": "前回は有効な応答本文がありませんでした。出力契約どおり JSON object 全体を再生成してください。",
                },
            ]
            continue
        try:
            interpretation = parse_request_interpretation(completion.response.content, request.content)
            break
        except StoryPipelineError as error:
            if error.exit_code != 7 or logical_call >= maximum_calls:
                raise
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "前回の応答は出力契約に違反しました。応答全文を再利用せず、"
                        f"次を修正して JSON object 全体を再生成してください: {error.reason}"
                    ),
                },
            ]
    assert completion is not None and interpretation is not None
    decisions = resolve_pending_decisions(state, interpretation)
    scope = determine_work_scope(root, state, interpretation)
    return PlannedRequest(request, interpretation, decisions, scope, completion, logical_call)
