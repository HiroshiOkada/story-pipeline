"""要求実行記録の読み込みと整合性検証。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from story_pipeline.validation import IssueCollector


RUN_FILENAME = re.compile(r"^([0-9]{4})\.json$")
HASH = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
RUN_STATUSES = {"running", "completed", "failed", "awaiting_human"}
STEP_STATUSES = {"pending", "running", "completed", "failed", "skipped"}
RUN_KEYS = {
    "schema_version", "request_number", "status", "started_at", "updated_at",
    "finished_at", "request_sha256", "start_commit", "end_commit", "current_step",
    "steps", "call_counts", "model_attempts", "input_hashes", "output_hashes",
    "restored_files", "fallbacks", "errors", "resume",
}


class RunFormatError(ValueError):
    def __init__(self, message: str, location: str) -> None:
        super().__init__(message)
        self.location = location


def validate_runs(
    root: Path, state: dict[str, Any] | None, collector: IssueCollector
) -> dict[int, dict[str, Any]]:
    """全 run JSON を検証し、読み込めた記録を番号別に返す。"""
    runs: dict[int, dict[str, Any]] = {}
    directory = root / ".story-pipeline" / "runs"
    if not directory.exists():
        if state and (state["last_request"] is not None or state["active_request"] is not None):
            collector.error("RUN_DIRECTORY_MISSING", "実行記録ディレクトリがありません", ".story-pipeline/runs")
        return runs
    if not _safe_directory(root, directory):
        collector.error("RUN_DIRECTORY_INVALID", "実行記録パスが安全なディレクトリではありません", ".story-pipeline/runs")
        return runs
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        collector.error("RUN_DIRECTORY_UNREADABLE", "実行記録ディレクトリを読み取れません", ".story-pipeline/runs")
        return runs
    for path in entries:
        match = RUN_FILENAME.fullmatch(path.name)
        if match is None:
            collector.warning("UNKNOWN_RUN_FILE", "実行記録の命名規則に一致しません", path.relative_to(root).as_posix())
            continue
        number = int(match.group(1))
        relative = path.relative_to(root).as_posix()
        if not _safe_file(root, path):
            collector.error("RUN_FILE_INVALID", "安全な通常ファイルではありません", relative)
            continue
        try:
            run = _load_run(path, number)
        except (OSError, UnicodeError, json.JSONDecodeError, RunFormatError, ValueError) as error:
            location = error.location if isinstance(error, RunFormatError) else relative
            collector.error("RUN_SCHEMA_INVALID", str(error), location)
            continue
        runs[number] = run
        _validate_recorded_hashes(root, run, relative, collector)
    _validate_state_references(state, runs, collector)
    return runs


def _load_run(path: Path, filename_number: int) -> dict[str, Any]:
    run = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    location = path.as_posix()
    run = _object(run, location)
    _keys(run, RUN_KEYS, location)
    if _integer(run["schema_version"], f"{location}#/schema_version") != 1:
        _fail("schema_version は 1 である必要があります", f"{location}#/schema_version")
    number = _bounded_integer(run["request_number"], f"{location}#/request_number", 0, 9999)
    if number != filename_number:
        _fail("request_number がファイル名と一致しません", f"{location}#/request_number")
    status = _enum(run["status"], RUN_STATUSES, f"{location}#/status")
    _timestamp(run["started_at"], f"{location}#/started_at")
    _timestamp(run["updated_at"], f"{location}#/updated_at")
    finished = run["finished_at"]
    if finished is not None:
        _timestamp(finished, f"{location}#/finished_at")
    if status == "running" and finished is not None:
        _fail("running では finished_at は null です", f"{location}#/finished_at")
    if status != "running" and finished is None:
        _fail("終了状態では finished_at が必要です", f"{location}#/finished_at")
    _hash(run["request_sha256"], f"{location}#/request_sha256")
    _pattern_string(run["start_commit"], COMMIT, f"{location}#/start_commit")
    if run["end_commit"] is not None:
        _pattern_string(run["end_commit"], COMMIT, f"{location}#/end_commit")
    _string(run["current_step"], f"{location}#/current_step")
    _validate_steps(run["steps"], location)
    counts = _object(run["call_counts"], f"{location}#/call_counts")
    _keys(counts, {"generation", "review", "revision", "summary"}, f"{location}#/call_counts")
    for name, value in counts.items():
        _bounded_integer(value, f"{location}#/call_counts/{name}", 0, 2**63 - 1)
    for name in ("model_attempts", "fallbacks"):
        for index, item in enumerate(_array(run[name], f"{location}#/{name}")):
            _object(item, f"{location}#/{name}/{index}")
    _hash_map(run["input_hashes"], f"{location}#/input_hashes")
    _hash_map(run["output_hashes"], f"{location}#/output_hashes")
    for index, item in enumerate(_array(run["restored_files"], f"{location}#/restored_files")):
        _relative_path(_string(item, f"{location}#/restored_files/{index}"), f"{location}#/restored_files/{index}")
    _validate_errors(run["errors"], location)
    _validate_resume(run["resume"], location)
    return run


def _validate_steps(value: Any, location: str) -> None:
    identifiers: set[str] = set()
    for index, item in enumerate(_array(value, f"{location}#/steps")):
        item_location = f"{location}#/steps/{index}"
        step = _object(item, item_location)
        _keys(step, {"id", "status", "started_at", "finished_at", "input_hashes", "output_hashes", "result"}, item_location)
        identifier = _string(step["id"], f"{item_location}/id")
        if not identifier or identifier in identifiers:
            _fail("工程 ID は空でない一意な値が必要です", f"{item_location}/id")
        identifiers.add(identifier)
        status = _enum(step["status"], STEP_STATUSES, f"{item_location}/status")
        if step["started_at"] is not None:
            _timestamp(step["started_at"], f"{item_location}/started_at")
        if step["finished_at"] is not None:
            _timestamp(step["finished_at"], f"{item_location}/finished_at")
        if status == "completed" and step["finished_at"] is None:
            _fail("completed 工程には finished_at が必要です", f"{item_location}/finished_at")
        _hash_map(step["input_hashes"], f"{item_location}/input_hashes")
        _hash_map(step["output_hashes"], f"{item_location}/output_hashes")
        if step["result"] is not None:
            _string(step["result"], f"{item_location}/result")


def _validate_errors(value: Any, location: str) -> None:
    for index, item in enumerate(_array(value, f"{location}#/errors")):
        item_location = f"{location}#/errors/{index}"
        error = _object(item, item_location)
        _keys(error, {"step", "category", "message", "retryable", "occurred_at"}, item_location)
        for key in ("step", "category", "message"):
            _string(error[key], f"{item_location}/{key}")
        if not isinstance(error["retryable"], bool):
            _fail("boolean である必要があります", f"{item_location}/retryable")
        _timestamp(error["occurred_at"], f"{item_location}/occurred_at")


def _validate_resume(value: Any, location: str) -> None:
    if value is None:
        return
    resume = _object(value, f"{location}#/resume")
    _keys(resume, {"step", "reason"}, f"{location}#/resume")
    _string(resume["step"], f"{location}#/resume/step")
    _string(resume["reason"], f"{location}#/resume/reason")


def _validate_recorded_hashes(
    root: Path, run: dict[str, Any], run_location: str, collector: IssueCollector
) -> None:
    request = f"requests/{run['request_number']:04d}.md"
    _check_hash(root, request, run["request_sha256"], "REQUEST_HASH", collector)
    for section in ("input_hashes", "output_hashes"):
        for relative, expected in run[section].items():
            _check_hash(root, relative, expected, "RECORDED_HASH", collector, run_location)


def _check_hash(
    root: Path,
    relative_text: str,
    expected: str,
    code_prefix: str,
    collector: IssueCollector,
    source: str = "",
) -> None:
    try:
        relative = _relative_path(relative_text, source or relative_text)
        path = root / relative
        target = path.resolve(strict=True)
        target.relative_to(root)
        if not target.is_file():
            raise OSError
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
    except (OSError, ValueError):
        collector.error(f"{code_prefix}_FILE_MISSING", "ハッシュ対象の安全な通常ファイルがありません", relative_text)
        return
    if actual != expected:
        collector.error(f"{code_prefix}_MISMATCH", "記録済み SHA-256 と内容が一致しません", relative_text)


def _validate_state_references(
    state: dict[str, Any] | None,
    runs: dict[int, dict[str, Any]],
    collector: IssueCollector,
) -> None:
    if state is None:
        return
    active = state["active_request"]
    if active is not None:
        if active not in runs:
            collector.error("STATE_ACTIVE_RUN_MISSING", "active_request の実行記録がありません", f".story-pipeline/runs/{active:04d}.json")
        elif runs[active]["status"] not in {"running", "failed"}:
            collector.error("STATE_ACTIVE_RUN_STATUS", "active_request の状態は running または failed である必要があります", f".story-pipeline/runs/{active:04d}.json")
    last = state["last_request"]
    if last is not None and last not in runs:
        collector.error("STATE_LAST_RUN_MISSING", "last_request の実行記録がありません", f".story-pipeline/runs/{last:04d}.json")
    if last is not None and runs and last > max(runs):
        collector.error("STATE_LAST_RUN_RANGE", "last_request が実行記録の最大番号を超えています", ".story-pipeline/state.json#/last_request")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重複したキーです: {key}")
        result[key] = value
    return result


def _keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    difference = expected ^ value.keys()
    if difference:
        key = sorted(difference)[0]
        kind = "必須キーがありません" if key in expected else "未知のキーです"
        _fail(kind, f"{location}/{key}")


def _hash_map(value: Any, location: str) -> None:
    mapping = _object(value, location)
    for key, item in mapping.items():
        _relative_path(key, f"{location}/{key}")
        _hash(item, f"{location}/{key}")


def _relative_path(value: str, location: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        _fail("作品ルート相対の安全なパスが必要です", location)
    return value


def _safe_directory(root: Path, path: Path) -> bool:
    try:
        target = path.resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError):
        return False
    return target.is_dir()


def _safe_file(root: Path, path: Path) -> bool:
    try:
        target = path.resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError):
        return False
    return target.is_file()


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("object である必要があります", location)
    return value


def _array(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("array である必要があります", location)
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str):
        _fail("string である必要があります", location)
    return value


def _integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("integer である必要があります", location)
    return value


def _bounded_integer(value: Any, location: str, minimum: int, maximum: int) -> int:
    number = _integer(value, location)
    if not minimum <= number <= maximum:
        _fail(f"{minimum}..{maximum} の範囲である必要があります", location)
    return number


def _enum(value: Any, choices: set[str], location: str) -> str:
    text = _string(value, location)
    if text not in choices:
        _fail("定義済みの値が必要です", location)
    return text


def _timestamp(value: Any, location: str) -> str:
    return _pattern_string(value, TIMESTAMP, location)


def _hash(value: Any, location: str) -> str:
    return _pattern_string(value, HASH, location)


def _pattern_string(value: Any, pattern: re.Pattern[str], location: str) -> str:
    text = _string(value, location)
    if pattern.fullmatch(text) is None:
        _fail("形式が不正です", location)
    return text


def _fail(message: str, location: str) -> None:
    raise RunFormatError(message, location)
