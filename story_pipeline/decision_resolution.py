"""後続要求による pending decision の明示的な解決。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from story_pipeline.errors import StoryPipelineError
from story_pipeline.request_interpretation import RequestInterpretation


@dataclass(frozen=True, slots=True)
class DecisionResolution:
    id: str
    answer: str
    originating_request: int


def resolve_pending_decisions(
    state: dict[str, Any], interpretation: RequestInterpretation
) -> tuple[DecisionResolution, ...]:
    """解釈された回答を pending 集合と完全一致で照合する。"""
    pending = {item["id"]: item for item in state["pending_decisions"]}
    supplied = {item.id: item.answer for item in interpretation.decision_answers}
    if not pending:
        if supplied:
            raise _decision_error("未解決でない判断 ID への回答が含まれています")
        return ()
    if interpretation.kind not in {"answer", "mixed"}:
        raise _decision_error("未解決の判断に回答する要求として解釈されていません")
    missing = pending.keys() - supplied.keys()
    if missing:
        raise _decision_error(f"必要な判断 ID への回答がありません: {sorted(missing)[0]}")
    unknown = supplied.keys() - pending.keys()
    if unknown:
        raise _decision_error(f"未知または解決済みの判断 ID です: {sorted(unknown)[0]}")
    resolutions: list[DecisionResolution] = []
    for identifier in pending:
        decision = pending[identifier]
        answer = supplied[identifier]
        if answer not in decision["choices"]:
            raise _decision_error(f"提示された選択肢と一致しない回答です: {identifier}")
        resolutions.append(DecisionResolution(identifier, answer, decision["request"]))
    return tuple(resolutions)


def _decision_error(reason: str) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        ".story-pipeline/state.json#/pending_decisions",
        "要求へ判断 ID と提示された選択肢のいずれかを明記してください",
        8,
    )
