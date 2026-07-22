"""`story-pipeline run` の終了境界と利用者向け出力。"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from story_pipeline.git_safety import commit_run_outputs
from story_pipeline.next_request import create_next_request
from story_pipeline.request_selection import has_meaningful_request_content
from story_pipeline.run_execution import RunExecutionResult, execute_started_run
from story_pipeline.run_report import FileChange, ReportContext, write_run_report
from story_pipeline.run_start import prepare_run


def run_command(*, output: TextIO, error_output: TextIO) -> int:
    """1要求を開始から報告、commit、次要求作成まで処理する。"""
    start = prepare_run(output=output)
    if start is None:
        print("No pending request.", file=output)
        return 0
    try:
        result = execute_started_run(start)
        report = _write_report(start.root, result)
        managed = tuple(dict.fromkeys((
            *result.changed_files,
            ".story-pipeline/state.json",
            f".story-pipeline/runs/{start.request.number:04d}.json",
            report,
        )))
        commit_run_outputs(
            start.root,
            start.request.number,
            result.run["status"].replace("_", "-"),
            managed,
            body=(f"Phase: {result.workflow.phase if result.workflow else 'unknown'}",),
        )
        next_request = _next_request(start.root, start.request.number)
        _print_result(result, report, next_request, output, error_output)
        return result.exit_code
    finally:
        start.lock.release()


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
    print(f"Report: {report}", file=target)
    print(f"Next request: {next_request}", file=target)
