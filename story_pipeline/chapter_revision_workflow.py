"""章の評価、局所改稿、再評価、完成更新候補を接続する。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from story_pipeline.chapter_revision import (
    DEFAULT_CHAPTER_REVISION_CONTEXT,
    ChapterCompletionUpdate,
    ChapterEvaluation,
    ChapterRevisionCandidate,
    ChapterRevisionContext,
    EvaluatedChapterRevision,
    build_chapter_completion_update,
    build_chapter_revision_context,
    build_chapter_revision_messages,
    build_chapter_summary_messages,
    chapter_evaluation_response_format,
    chapter_revision_response_format,
    chapter_summary_response_format,
    check_chapter_revision_candidate,
    parse_chapter_evaluation,
    parse_chapter_revision_candidate,
    select_best_chapter_revision,
)
from story_pipeline.errors import StoryPipelineError
from story_pipeline.llm_client import CompletionResult, LLMClient
from story_pipeline.llm_transport import ApiFailure
from story_pipeline.request_interpretation import RequestInterpretation
from story_pipeline.request_selection import SelectedRequest


@dataclass(frozen=True, slots=True)
class ChapterRevisionCall:
    role: str
    purpose: str
    completion: CompletionResult


@dataclass(frozen=True, slots=True)
class ChapterRevisionWorkflowResult:
    status: str
    context: ChapterRevisionContext
    candidates: tuple[EvaluatedChapterRevision, ...]
    best: EvaluatedChapterRevision | None
    completion_update: ChapterCompletionUpdate | None
    calls: tuple[ChapterRevisionCall, ...]
    call_counts: tuple[tuple[str, int], ...]
    reason: str | None


def produce_chapter_revision(
    root: Path,
    request: SelectedRequest,
    interpretation: RequestInterpretation,
    chapter_number: int,
    client: LLMClient,
    *,
    completed_chapters: tuple[int, ...] = (),
    all_chapters_complete: bool = False,
    context_paths: list[str] | tuple[str, ...] = DEFAULT_CHAPTER_REVISION_CONTEXT,
) -> ChapterRevisionWorkflowResult:
    """章を評価し、上限内の局所改稿後に完成更新候補を返す。"""
    context = build_chapter_revision_context(
        root, request, interpretation, chapter_number, context_paths=context_paths
    )
    originals = tuple(
        (path, (root / path).read_text(encoding="utf-8")) for path in context.episode_paths
    )
    calls: list[ChapterRevisionCall] = []
    counts = {"review": 0, "revision": 0, "summary": 0}
    review_limit = client.config["limits"]["review_calls"]
    if review_limit < 2:
        return _result("failed", context, (), calls, counts, "章評価とあらすじ検証には review_calls が2回以上必要です")
    evaluation = _evaluate(
        client, list(context.messages), review_limit - 1, calls, counts
    )
    if evaluation is None:
        return _result("failed", context, (), calls, counts, "章を有効な形式で評価できませんでした")
    records = [EvaluatedChapterRevision(None, originals, evaluation)]
    while (
        not records[-1].evaluation.adoptable
        and records[-1].evaluation.decision != "awaiting_human"
        and counts["revision"] < client.config["limits"]["revision_calls"]
        and counts["review"] < review_limit - 1
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
            review_limit - 1 - counts["review"], calls, counts,
        )
        if evaluation is None:
            break
        records.append(EvaluatedChapterRevision(candidate, documents, evaluation))
    best = select_best_chapter_revision(records)
    if best is not None:
        update = _summary(
            client, context, best, root, completed_chapters, all_chapters_complete,
            review_limit - counts["review"], calls, counts,
        )
        if update is None:
            return _result(
                "failed", context, tuple(records), calls, counts,
                "完成章のあらすじを有効な根拠付き形式で生成できませんでした", best,
            )
        return _result("completed", context, tuple(records), calls, counts, None, best, update)
    if records[-1].evaluation.decision == "awaiting_human":
        return _result(
            "awaiting_human", context, tuple(records), calls, counts,
            records[-1].evaluation.summary or "章の改稿に人間の判断が必要です",
        )
    return _result(
        "failed", context, tuple(records), calls, counts,
        "呼び出し上限内に完成判定済みの章を得られませんでした",
    )


def _evaluate(
    client: LLMClient,
    messages: list[dict[str, str]],
    maximum_calls: int,
    calls: list[ChapterRevisionCall],
    counts: dict[str, int],
) -> ChapterEvaluation | None:
    for _ in range(maximum_calls):
        counts["review"] += 1
        completion = _complete(client, "reviewer", messages, chapter_evaluation_response_format())
        if completion is None:
            messages.append({"role": "user", "content": "評価 JSON object 全体を再生成してください。"})
            continue
        calls.append(ChapterRevisionCall("reviewer", "review", completion))
        try:
            return parse_chapter_evaluation(completion.response.content)
        except StoryPipelineError as error:
            if error.exit_code != 7:
                raise
            messages.append({"role": "user", "content": f"評価契約違反を直して全体を再生成してください: {error.reason}"})
    return None


def _revise(
    client: LLMClient,
    context: ChapterRevisionContext,
    current: EvaluatedChapterRevision,
    maximum_calls: int,
    generation: int,
    calls: list[ChapterRevisionCall],
    counts: dict[str, int],
) -> tuple[ChapterRevisionCandidate, tuple[tuple[str, str], ...]] | None:
    messages = list(build_chapter_revision_messages(context, current))
    for _ in range(maximum_calls):
        counts["revision"] += 1
        completion = _complete(client, "reviser", messages, chapter_revision_response_format())
        if completion is None:
            messages.append({"role": "user", "content": "局所改稿 JSON object 全体を再生成してください。"})
            continue
        calls.append(ChapterRevisionCall("reviser", "revision", completion))
        try:
            candidate = parse_chapter_revision_candidate(
                completion.response.content, generation=generation,
                model_reference=completion.model_reference, input_hashes=context.input_hashes,
                revision_count=counts["revision"],
            )
        except StoryPipelineError as error:
            if error.exit_code != 7:
                raise
            messages.append({"role": "user", "content": f"改稿契約違反を直して全体を再生成してください: {error.reason}"})
            continue
        checked = check_chapter_revision_candidate(candidate, context, current.documents)
        if checked.accepted:
            return candidate, checked.documents
        issues = "、".join(f"{issue.code}: {issue.location}" for issue in checked.issues)
        messages.append({"role": "user", "content": f"機械検査違反を直して局所改稿全体を再生成してください: {issues}"})
    return None


def _summary(
    client: LLMClient,
    context: ChapterRevisionContext,
    best: EvaluatedChapterRevision,
    root: Path,
    completed_chapters: tuple[int, ...],
    all_chapters_complete: bool,
    maximum_calls: int,
    calls: list[ChapterRevisionCall],
    counts: dict[str, int],
) -> ChapterCompletionUpdate | None:
    messages = list(build_chapter_summary_messages(context, best))
    for _ in range(maximum_calls):
        counts["summary"] += 1
        completion = _complete(client, "reviewer", messages, chapter_summary_response_format())
        if completion is None:
            messages.append({"role": "user", "content": "章あらすじ JSON object 全体を再生成してください。"})
            continue
        calls.append(ChapterRevisionCall("reviewer", "summary", completion))
        try:
            return build_chapter_completion_update(
                completion.response.content, context=context, accepted=best,
                chapter_content=(root / context.chapter_path).read_text(encoding="utf-8"),
                completed_chapters=completed_chapters,
                all_chapters_complete=all_chapters_complete,
            )
        except StoryPipelineError as error:
            if error.exit_code != 7:
                raise
            messages.append({"role": "user", "content": f"根拠検証違反を直して全体を再生成してください: {error.reason}"})
    return None


def _evaluation_messages(
    context: ChapterRevisionContext,
    documents: tuple[tuple[str, str], ...],
) -> list[dict[str, str]]:
    data = json.dumps(
        [{"path": path, "content": content} for path, content in documents],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
    return [
        *context.messages[:4],
        {"role": "user", "content": f"改稿後の章内本文:\n--- BEGIN REVISED CHAPTER sha256={digest} ---\n{data}\n--- END REVISED CHAPTER sha256={digest} ---"},
        {"role": "user", "content": "8観点を再評価し、complete を改めて判定してください。"},
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
    context: ChapterRevisionContext,
    candidates: tuple[EvaluatedChapterRevision, ...],
    calls: list[ChapterRevisionCall],
    counts: dict[str, int],
    reason: str | None,
    best: EvaluatedChapterRevision | None = None,
    completion_update: ChapterCompletionUpdate | None = None,
) -> ChapterRevisionWorkflowResult:
    return ChapterRevisionWorkflowResult(
        status, context, candidates, best, completion_update, tuple(calls),
        tuple(sorted(counts.items())), reason,
    )

