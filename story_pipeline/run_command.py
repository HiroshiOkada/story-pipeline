"""`story-pipeline run` の終了境界と利用者向け出力。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TextIO
import uuid

from story_pipeline.git_safety import commit_run_outputs
from story_pipeline.next_request import create_next_request
from story_pipeline.request_selection import has_meaningful_request_content
from story_pipeline.run_execution import RunExecutionResult, execute_started_run
from story_pipeline.run_report import FileChange, ReportContext, write_run_report
from story_pipeline.execution_store import persist_run_progress
from story_pipeline.run_lifecycle import (
    record_incident,
    record_operational_error,
    transition_lifecycle,
)
from story_pipeline.errors import StoryPipelineError
from story_pipeline.run_start import prepare_run
from story_pipeline.interruptions import TerminationSignal, capture_sigterm


def run_command(*, output: TextIO, error_output: TextIO) -> int:
    """1要求を開始から報告、commit、次要求作成まで処理する。"""
    start = prepare_run(output=output)
    if start is None:
        print("No pending request.", file=output)
        return 0
    if hasattr(start.client, "event_sink"):
        start.client.event_sink = lambda event: print(
            json.dumps(event, ensure_ascii=False, sort_keys=True), file=error_output, flush=True
        )
    result: RunExecutionResult | None = None
    report: str | None = None
    next_request: str | None = None
    code = 9
    termination = capture_sigterm()
    termination.__enter__()
    try:
        result = execute_started_run(start)
        try:
            report = _write_report(start.root, result)
        except BaseException as error:
            result = _finalization_failure(
                start.root, result, error, _incident_component(error, "finalizing"), "write_report"
            )
            _print_incident(result, error_output)
            return _interruption_code(error) or 9
        managed = tuple(dict.fromkeys((
            *result.changed_files,
            ".story-pipeline/state.json",
            f".story-pipeline/runs/{start.request.number:04d}.json",
            report,
        )))
        result = RunExecutionResult(
            transition_lifecycle(result.run, "committed"), result.state, result.planned,
            result.workflow, result.changed_files, result.reason, result.exit_code,
        )
        persist_run_progress(start.root, result.run)
        try:
            commit_run_outputs(
                start.root,
                start.request.number,
                result.run["status"].replace("_", "-"),
                managed,
                body=(f"Phase: {result.workflow.phase if result.workflow else 'unknown'}",),
            )
        except BaseException as error:
            result = _finalization_failure(
                start.root, result, error, _incident_component(error, "git"), "commit_outputs"
            )
            _print_incident(result, error_output)
            return _interruption_code(error) or 9
        try:
            next_request = _next_request(start.root, start.request.number)
        except BaseException as error:
            result = _finalization_failure(
                start.root, result, error, _incident_component(error, "finalizing"), "next_request"
            )
            _print_incident(result, error_output)
            return _interruption_code(error) or 9
        _print_result(result, report, next_request, output, error_output)
        code = result.exit_code
    finally:
        try:
            start.lock.release()
        except BaseException as error:
            if result is not None:
                result = _finalization_failure(
                    start.root, result, error, _incident_component(error, "lock"), "release_lock"
                )
                _print_incident(result, error_output)
            code = _interruption_code(error) or 9
        finally:
            termination.__exit__(None, None, None)
    return code


def _finalization_failure(
    root: Path,
    result: RunExecutionResult,
    error: BaseException,
    component: str,
    step: str,
) -> RunExecutionResult:
    run = record_incident(
        result.run,
        incident_id=f"inc-{uuid.uuid4().hex}",
        component=component,
        exception_class=type(error).__name__,
        step=step,
        retryable=False,
    )
    safe_reason = "予期しない内部エラーが発生しました"
    if isinstance(error, StoryPipelineError):
        safe_reason = error.reason
        run = record_operational_error(
            run,
            step=step,
            category=component,
            message=safe_reason,
            retryable=False,
        )
    persist_run_progress(root, run)
    return RunExecutionResult(
        run, result.state, result.planned, result.workflow, result.changed_files,
        safe_reason,
        _interruption_code(error) or 9,
    )


def _print_incident(result: RunExecutionResult, output: TextIO) -> None:
    incident = result.run["incidents"][-1]
    print(f"Error: {result.reason}", file=output)
    print(f"Incident: {incident['incident_id']}", file=output)
    print(f"Component: {incident['component']}", file=output)


def _interruption_code(error: BaseException) -> int | None:
    if isinstance(error, KeyboardInterrupt):
        return 130
    if isinstance(error, TerminationSignal):
        return 143
    return None


def _incident_component(error: BaseException, default: str) -> str:
    return "interruption" if _interruption_code(error) is not None else default


def _write_report(root: Path, result: RunExecutionResult) -> str:
    planned = result.planned
    workflow = result.workflow
    interpretation = planned.interpretation if planned is not None else None
    context = ReportContext(
        request_summary=(interpretation.summary if interpretation is not None else "解釈工程で終了"),
        kind=(interpretation.kind if interpretation is not None else "unknown"),
        targets=(planned.scope.targets if planned is not None else ()),
        assumptions=(),
        changed_files=tuple(FileChange(path, "作成または更新") for path in result.changed_files),
        adoption_reason=(workflow.evaluation if workflow is not None else None),
        unresolved=((result.reason,) if result.reason else ()),
        next_action=("同じ要求を再実行" if result.run["status"] == "failed" else "次要求を記入"),
    )
    return write_run_report(root, result.run, context)


def _next_request(root: Path, current: int) -> str:
    requests = root / "requests"
    for path in sorted(requests.glob("[0-9][0-9][0-9][0-9].md")):
        if int(path.stem) > current and not has_meaningful_request_content(path.read_text(encoding="utf-8")):
            return path.relative_to(root).as_posix()
    return create_next_request(root)


def _print_result(
    result: RunExecutionResult,
    report: str,
    next_request: str,
    output: TextIO,
    error_output: TextIO,
) -> None:
    target = error_output if result.exit_code else output
    print(f"Request: {result.run['request_number']:04d}", file=target)
    print(f"Status: {result.run['status']}", file=target)
    if result.reason:
        print(f"Reason: {result.reason}", file=target)
    print("Changed files: " + (", ".join(result.changed_files) or "none"), file=target)
    models = sorted({(item["role"], item["api_model"]) for item in result.run["model_attempts"]})
    print("Models: " + (", ".join(f"{role}={model}" for role, model in models) or "none"), file=target)
    counts = result.run["call_counts"]
    names = ("generation", "review", "revision", "knowledge", "summary")
    print("Calls: " + ", ".join(f"{name}={counts.get(name, 0)}" for name in names), file=target)
    metrics = result.run.get("metrics")
    if isinstance(metrics, dict):
        print(
            "Performance: "
            f"logical={metrics['logical_calls']}, transport={metrics['transport_attempts']}, "
            f"retry_wait_ms={metrics['retry_wait_ms']}, elapsed_ms={metrics['elapsed_ms']}",
            file=target,
        )
    print(f"Report: {report}", file=target)
    print(f"Next request: {next_request}", file=target)
