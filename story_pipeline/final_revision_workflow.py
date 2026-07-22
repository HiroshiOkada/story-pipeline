"""小説全体の評価、局所改稿、再評価、完成判定を接続する。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from story_pipeline.errors import StoryPipelineError
from story_pipeline.final_revision import (
    DEFAULT_FINAL_REVISION_CONTEXT,
    EvaluatedFinalRevision,
    FinalCompletionUpdate,
    FinalEvaluation,
    FinalRevisionCandidate,
    FinalRevisionContext,
    build_final_completion_update,
    build_final_revision_context,
    build_final_revision_messages,
    check_final_revision_candidate,
    final_evaluation_response_format,
    final_revision_response_format,
    parse_final_evaluation,
    parse_final_revision_candidate,
    select_best_final_revision,
)
from story_pipeline.llm_client import CompletionResult, LLMClient
from story_pipeline.llm_transport import ApiFailure
from story_pipeline.request_interpretation import RequestInterpretation
from story_pipeline.request_selection import SelectedRequest


@dataclass(frozen=True, slots=True)
class FinalRevisionCall:
    role: str
    purpose: str
    completion: CompletionResult


@dataclass(frozen=True, slots=True)
class FinalRevisionWorkflowResult:
    status: str
    context: FinalRevisionContext
    candidates: tuple[EvaluatedFinalRevision, ...]
    best: EvaluatedFinalRevision | None
    completion_update: FinalCompletionUpdate | None
    calls: tuple[FinalRevisionCall, ...]
    call_counts: tuple[tuple[str, int], ...]
    reason: str | None


def produce_final_revision(
    root: Path,
    request: SelectedRequest,
    interpretation: RequestInterpretation,
    client: LLMClient,
    *,
    completed_chapters: tuple[int, ...],
    completed_episodes: tuple[int, ...],
    pending_reviews: tuple[object, ...] = (),
    pending_decisions: tuple[object, ...] = (),
    max_full_text_characters: int = 200_000,
    context_paths: list[str] | tuple[str, ...] = DEFAULT_FINAL_REVISION_CONTEXT,
) -> FinalRevisionWorkflowResult:
    """作品全体を評価し、局所改稿後に明示的な完成更新候補を返す。"""
    context = build_final_revision_context(
        root, request, interpretation,
        max_full_text_characters=max_full_text_characters, context_paths=context_paths,
    )
    originals = tuple(
        (path, (root / path).read_text(encoding="utf-8")) for path in context.episode_paths
    )
    calls: list[FinalRevisionCall] = []
    counts = {"review": 0, "revision": 0}
    evaluation = _evaluate(
        client, list(context.messages), client.config["limits"]["review_calls"], calls, counts
    )
    if evaluation is None:
        return _result("failed", context, (), calls, counts, "小説全体を有効な形式で評価できませんでした")
    records = [EvaluatedFinalRevision(None, originals, evaluation)]
    if context.mode != "full_text" and evaluation.decision == "revise":
        return _result(
            "awaiting_human", context, tuple(records), calls, counts,
            "章要約モードの評価で本文改稿が必要になりました。全本文を扱える設定で再実行してください",
        )
    while (
        not records[-1].evaluation.adoptable
        and records[-1].evaluation.decision != "awaiting_human"
        and counts["revision"] < client.config["limits"]["revision_calls"]
        and counts["review"] < client.config["limits"]["review_calls"]
    ):
        revised = _revise(
            client, context, records[-1],
            client.config["limits"]["revision_calls"] - counts["revision"],
            len(records), calls, counts,
        )
        if revised is None:
            break
        candidate, documents = revised
        evaluation = _evaluate(
            client, _evaluation_messages(context, documents),
            client.config["limits"]["review_calls"] - counts["review"], calls, counts,
        )
        if evaluation is None:
            break
        records.append(EvaluatedFinalRevision(candidate, documents, evaluation))
    best = select_best_final_revision(records)
    if best is not None:
        try:
            update = build_final_completion_update(
                context, best, completed_chapters=completed_chapters,
                completed_episodes=completed_episodes, pending_reviews=pending_reviews,
                pending_decisions=pending_decisions,
            )
        except ValueError as error:
            return _result("failed", context, tuple(records), calls, counts, str(error), best)
        return _result("completed", context, tuple(records), calls, counts, None, best, update)
    if records[-1].evaluation.decision == "awaiting_human":
        return _result(
            "awaiting_human", context, tuple(records), calls, counts,
            records[-1].evaluation.summary or "小説の完成に人間の判断が必要です",
        )
    return _result(
        "failed", context, tuple(records), calls, counts,
        "呼び出し上限内に完成判定済みの小説を得られませんでした",
    )


def _evaluate(
    client: LLMClient,
    messages: list[dict[str, str]],
    maximum_calls: int,
    calls: list[FinalRevisionCall],
    counts: dict[str, int],
) -> FinalEvaluation | None:
    for _ in range(maximum_calls):
        counts["review"] += 1
        completion = _complete(client, "reviewer", messages, final_evaluation_response_format())
        if completion is None:
            messages.append({"role": "user", "content": "全体評価 JSON object 全体を再生成してください。"})
            continue
        calls.append(FinalRevisionCall("reviewer", "review", completion))
        try:
            return parse_final_evaluation(completion.response.content)
        except StoryPipelineError as error:
            if error.exit_code != 7:
                raise
            messages.append({"role": "user", "content": f"評価契約違反を直して全体を再生成してください: {error.reason}"})
    return None


def _revise(
    client: LLMClient,
    context: FinalRevisionContext,
    current: EvaluatedFinalRevision,
    maximum_calls: int,
    generation: int,
    calls: list[FinalRevisionCall],
    counts: dict[str, int],
) -> tuple[FinalRevisionCandidate, tuple[tuple[str, str], ...]] | None:
    messages = list(build_final_revision_messages(context, current))
    for _ in range(maximum_calls):
        counts["revision"] += 1
        completion = _complete(client, "reviser", messages, final_revision_response_format())
        if completion is None:
            messages.append({"role": "user", "content": "局所改稿 JSON object 全体を再生成してください。"})
            continue
        calls.append(FinalRevisionCall("reviser", "revision", completion))
        try:
            candidate = parse_final_revision_candidate(
                completion.response.content, generation=generation,
                model_reference=completion.model_reference, input_hashes=context.input_hashes,
                revision_count=counts["revision"],
            )
        except StoryPipelineError as error:
            if error.exit_code != 7:
                raise
            messages.append({"role": "user", "content": f"改稿契約違反を直して全体を再生成してください: {error.reason}"})
            continue
        checked = check_final_revision_candidate(candidate, context, current.documents)
        if checked.accepted:
            return candidate, checked.documents
        issues = "、".join(f"{issue.code}: {issue.location}" for issue in checked.issues)
        messages.append({"role": "user", "content": f"機械検査違反を直して全体を再生成してください: {issues}"})
    return None


def _evaluation_messages(
    context: FinalRevisionContext,
    documents: tuple[tuple[str, str], ...],
) -> list[dict[str, str]]:
    data = json.dumps(
        [{"path": path, "content": content} for path, content in documents],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
    return [
        *context.messages[:4],
        {"role": "user", "content": f"改稿後の全話本文:\n--- BEGIN REVISED NOVEL sha256={digest} ---\n{data}\n--- END REVISED NOVEL sha256={digest} ---"},
        {"role": "user", "content": "8観点を再評価し、小説全体の complete を改めて判定してください。"},
    ]


def _complete(
    client: LLMClient, role: str, messages: list[dict[str, str]], response_format: dict[str, object]
) -> CompletionResult | None:
    try:
        return client.complete_role(role, messages, response_format=response_format)
    except ApiFailure as error:
        if error.kind == "invalid_response":
            return None
        raise


def _result(
    status: str,
    context: FinalRevisionContext,
    candidates: tuple[EvaluatedFinalRevision, ...],
    calls: list[FinalRevisionCall],
    counts: dict[str, int],
    reason: str | None,
    best: EvaluatedFinalRevision | None = None,
    completion_update: FinalCompletionUpdate | None = None,
) -> FinalRevisionWorkflowResult:
    return FinalRevisionWorkflowResult(
        status, context, candidates, best, completion_update, tuple(calls),
        tuple(sorted(counts.items())), reason,
    )
