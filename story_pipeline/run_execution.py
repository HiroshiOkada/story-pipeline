"""開始済み run の工程実行、採用、終了永続化。"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path
from typing import Any
import uuid

from story_pipeline.errors import StoryPipelineError
from story_pipeline.interruptions import TerminationSignal
from story_pipeline.draft_checkpoint import (
    inspect_checkpoint_adoption,
    load_draft_checkpoint,
    mark_checkpoint_adopted,
    write_draft_checkpoint,
)
from story_pipeline.execution_store import persist_finished_execution, persist_run_progress
from story_pipeline.llm_transport import ApiFailure
from story_pipeline.persistence import atomic_write_text, sha256_file
from story_pipeline.request_planner import plan_selected_request
from story_pipeline.run_lifecycle import (
    finalize_run_record,
    finish_step,
    record_model_attempt,
    record_event,
    record_incident,
    start_step,
    transition_lifecycle,
    utc_timestamp,
)
from story_pipeline.run_start import RunStart
from story_pipeline.workflow_executor import WorkflowExecution, execute_planned_workflow


@dataclass(frozen=True, slots=True)
class RunExecutionResult:
    run: dict[str, Any]
    state: dict[str, Any]
    planned: Any | None
    workflow: WorkflowExecution | None
    changed_files: tuple[str, ...]
    reason: str | None
    exit_code: int
    action: str | None = None
    location: str | None = None


def execute_started_run(start: RunStart) -> RunExecutionResult:
    """固定工程を実行し、捕捉可能な失敗も終了記録へ確定する。"""
    run = start.run
    planned = None
    workflow = None
    changed: tuple[str, ...] = ()
    current = "interpret_request"
    try:
        run, current = _begin(start, run, "interpret_request", {
            start.request.relative_path: sha256_file(start.root / start.request.relative_path)
        })
        planned = plan_selected_request(start.root, start.state, start.request, start.client)
        run = _record_completion(start, run, "generation", "planner", planned.completion)
        run = _finish(start, run, current, "completed", result=planned.interpretation.summary)

        run, current = _begin(start, run, "determine_scope")
        run = _finish(start, run, current, "completed", result=f"{planned.scope.phase}: {planned.scope.action}")
        run, current = _begin(start, run, "build_context")
        run = _finish(start, run, current, "completed", result="制作コンテキストを検証")

        run, current = _begin(start, run, "generate")
        workflow = execute_planned_workflow(
            start.root,
            start.state,
            planned,
            start.client,
            request_revision=len(run["request_revisions"]) - 1,
        )
        for call in workflow.calls:
            run = _record_completion(start, run, call.purpose, call.role, call.completion)
        generate_status = "failed" if workflow.status == "failed" else "completed"
        run = _finish(start, run, current, generate_status, result=workflow.reason or workflow.evaluation)

        run = _derived_steps(start, run, workflow)
        changed = workflow.internal_files
        status = workflow.status
        documents = workflow.documents if status == "completed" else ()
        if documents and _changed_lines(start.root, documents) > start.config["limits"]["max_changed_lines"]:
            status = "awaiting_human"
            documents = ()
            workflow = WorkflowExecution(
                status,
                workflow.phase,
                (),
                {},
                workflow.calls,
                workflow.evaluation,
                "変更行数が設定上限を超えたため分割判断が必要です",
            )

        run, current = _begin(start, run, "adopt")
        if documents:
            changed_document_paths = _changed_document_paths(start.root, documents)
            for path, content in documents:
                atomic_write_text(start.root / path, content)
            output_hashes = {path: sha256_file(start.root / path) for path, _ in documents}
            if workflow.phase == "drafting" and workflow.internal_files:
                checkpoint = load_draft_checkpoint(start.root, start.request.number)
                if checkpoint is None or inspect_checkpoint_adoption(start.root, checkpoint) != "all":
                    raise StoryPipelineError(
                        "本文、canon、人物状態を同一採用単位として確認できません",
                        workflow.internal_files[0],
                        "checkpoint と出力 hash を検証してください",
                        4,
                    )
                checkpoint = mark_checkpoint_adopted(
                    checkpoint, checkpoint["adoption"]["output_hashes"]
                )
                write_draft_checkpoint(start.root, checkpoint)
            changed = tuple(dict.fromkeys((*changed, *changed_document_paths)))
            run = _finish(start, run, current, "completed", output_hashes=output_hashes, result="採用候補を保存")
        else:
            run = _finish(start, run, current, "skipped", result=workflow.reason or "採用変更なし")

        run, current = _begin(start, run, "update_state")
        run = _finish(start, run, current, "completed", result=f"status={status}")
        run, current = _begin(start, run, "write_report")
        run = _finish(start, run, current, "completed", result="終了処理で報告を保存")
        run = transition_lifecycle(run, "finalizing")
        run = finalize_run_record(
            run,
            status,
            resume_step="generate" if status == "failed" else None,
            resume_reason=workflow.reason if status == "failed" else None,
            error=(
                {"step": "generate", "category": "workflow", "message": workflow.reason or "制作失敗", "retryable": True}
                if status == "failed"
                else None
            ),
        )
        state = persist_finished_execution(
            start.root,
            start.state,
            run,
            state_updates=workflow.state_updates if status == "completed" else {},
        )
        return RunExecutionResult(
            run, state, planned, workflow, changed, workflow.reason, 8 if status == "awaiting_human" else 7 if status == "failed" else 0
        )
    except BaseException as error:
        run = _record_client_events(start, run, current)
        action = None
        location = None
        if isinstance(error, (KeyboardInterrupt, TerminationSignal)):
            code = 143 if isinstance(error, TerminationSignal) else 130
            message = "実行が割り込まれました"
            action = "作品の状態を validate で確認してから、同じ要求を再実行してください"
            component = "interruption"
        elif isinstance(error, StoryPipelineError):
            code = error.exit_code
            message = error.reason
            action = error.action
            location = error.location
            component = "workflow" if current == "generate" else "execution"
        elif isinstance(error, ApiFailure):
            code = 8 if error.awaiting_human else 7
            message = error.message
            component = "transport"
        else:
            code = 9
            message = "予期しない内部エラーが発生しました"
            component = "finalizing" if run.get("status") != "running" else (
                "workflow"
            )
        status = "awaiting_human" if code == 8 else "failed"
        run = record_incident(
            run,
            incident_id=f"inc-{uuid.uuid4().hex}",
            component=component,
            exception_class=type(error).__name__,
            step=current,
            retryable=code in {7, 9},
        )
        if run.get("status") == "running":
            run = _close_running_step(run, current, "failed", message)
            run = transition_lifecycle(run, "finalizing")
            run = finalize_run_record(
                run,
                status,
                resume_step=current if status == "failed" else None,
                resume_reason=message if status == "failed" else None,
                error={"step": current, "category": component, "message": message, "retryable": status == "failed"},
            )
            state = persist_finished_execution(start.root, start.state, run)
        else:
            persist_run_progress(start.root, run)
            state = start.state
        return RunExecutionResult(run, state, planned, workflow, (), message, code, action, location)


def _changed_document_paths(
    root: Path, documents: tuple[tuple[str, str], ...]
) -> tuple[str, ...]:
    """採用候補のうち、現在の作業ツリーと内容が異なるパスだけを返す。"""
    changed: list[str] = []
    for relative, content in documents:
        try:
            current = (root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            changed.append(relative)
            continue
        if current != content:
            changed.append(relative)
    return tuple(changed)


def _begin(start: RunStart, run: dict[str, Any], identifier: str, hashes: dict[str, str] | None = None) -> tuple[dict[str, Any], str]:
    actual = _unique_step(run, identifier)
    updated = start_step(run, actual, input_hashes=hashes)
    persist_run_progress(start.root, updated)
    return updated, actual


def _finish(start: RunStart, run: dict[str, Any], identifier: str, status: str, **kwargs: Any) -> dict[str, Any]:
    updated = finish_step(run, identifier, status, **kwargs)
    persist_run_progress(start.root, updated)
    return updated


def _record_completion(start: RunStart, run: dict[str, Any], category: str, role: str, completion: Any) -> dict[str, Any]:
    normalized = "summary" if category == "summary" else category
    if normalized not in {"generation", "review", "revision", "knowledge"}:
        normalized = "generation"
    run = _record_client_events(start, run, run["current_step"])
    timestamp = utc_timestamp()
    model = start.config["models"][completion.model_reference]["model"]
    fallbacks = tuple(
        {"source": item.source, "target": item.target, "reason": item.reason}
        for item in completion.fallbacks
    )
    updated = record_model_attempt(
        run,
        category=normalized,
        role=role,
        model_reference=completion.model_reference,
        api_model=model,
        started_at=timestamp,
        finished_at=timestamp,
        result="completed",
        attempts=completion.attempts,
        fallbacks=fallbacks,
        transport_attempts=tuple({
            "model_reference": item.model_reference,
            "api_model": item.api_model,
            "attempt": item.attempt,
            "maximum_attempts": item.maximum_attempts,
            "started_at": item.started_at,
            "finished_at": item.finished_at,
            "elapsed_ms": item.elapsed_ms,
            "result": item.result,
            "failure_kind": item.failure_kind,
            "wait_ms": item.wait_ms,
        } for item in completion.transport_attempts),
        usage=(
            {
                "prompt_tokens": completion.response.usage.prompt_tokens,
                "completion_tokens": completion.response.usage.completion_tokens,
                "total_tokens": completion.response.usage.total_tokens,
                "cached_tokens": completion.response.usage.cached_tokens,
                "reasoning_tokens": completion.response.usage.reasoning_tokens,
                "cost_usd": completion.response.usage.cost_usd,
            }
            if completion.response.usage is not None else None
        ),
        truncated=any(
            item.failure_kind == "output_truncated"
            for item in completion.transport_attempts
        ),
    )
    persist_run_progress(start.root, updated)
    return updated


def _record_client_events(
    start: RunStart, run: dict[str, Any], step: str
) -> dict[str, Any]:
    drain = getattr(start.client, "drain_events", None)
    if drain is None:
        return run
    updated = run
    for event in drain():
        updated = record_event(
            updated,
            kind=event["kind"],
            step=step,
            details=event["details"],
            now=event["occurred_at"],
        )
    if updated is not run:
        persist_run_progress(start.root, updated)
    return updated


def _derived_steps(start: RunStart, run: dict[str, Any], workflow: WorkflowExecution) -> dict[str, Any]:
    purposes = {call.purpose for call in workflow.calls}
    values = (
        ("mechanical_check", "completed" if workflow.calls else "skipped"),
        ("review", "completed" if "review" in purposes else "skipped"),
        ("revise", "completed" if "revision" in purposes else "skipped"),
        ("consistency_check", "completed" if workflow.status == "completed" else "skipped"),
    )
    for identifier, status in values:
        run, actual = _begin(start, run, identifier)
        run = _finish(start, run, actual, status)
    if workflow.phase == "drafting":
        diagnostic_by_boundary: dict[str, list[Any]] = {}
        for item in workflow.diagnostics:
            diagnostic_by_boundary.setdefault(item.boundary, []).append(item)
        failed_boundary = (
            workflow.diagnostics[-1].boundary
            if workflow.status == "failed" and workflow.diagnostics
            else None
        )
        for boundary in ("draft_json", "mechanical", "evaluation", "knowledge", "checkpoint"):
            items = diagnostic_by_boundary.get(boundary, [])
            result = "; ".join(
                f"{item.code}[{item.attempt}]: {item.reason}" for item in items
            ) or "検証エラーなし"
            run, actual = _begin(start, run, f"draft_{boundary}")
            status = "failed" if boundary == failed_boundary else "completed"
            run = _finish(start, run, actual, status, result=result)
    return run


def _close_running_step(run: dict[str, Any], identifier: str, status: str, result: str) -> dict[str, Any]:
    running = [item["id"] for item in run["steps"] if item["status"] == "running"]
    return finish_step(run, running[-1], status, result=result) if running else run


def _unique_step(run: dict[str, Any], base: str) -> str:
    existing = {item["id"] for item in run["steps"]}
    if base not in existing:
        return base
    index = 2
    while f"{base}_resume_{index}" in existing:
        index += 1
    return f"{base}_resume_{index}"


def _changed_lines(root: Path, documents: tuple[tuple[str, str], ...]) -> int:
    total = 0
    for relative, content in documents:
        path = root / relative
        before = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        after = content.splitlines()
        for tag, _a, _b, c, d in difflib.SequenceMatcher(a=before, b=after).get_opcodes():
            if tag in {"insert", "replace"}:
                total += d - c
    return total
