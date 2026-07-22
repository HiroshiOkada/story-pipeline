"""要求実行記録の読み込みと整合性検証。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import math
import re
from typing import Any

from story_pipeline.validation import IssueCollector


RUN_FILENAME = re.compile(r"^([0-9]{4})\.json$")
HASH = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
RUN_STATUSES = {"running", "completed", "failed", "awaiting_human"}
STEP_STATUSES = {"pending", "running", "completed", "failed", "skipped"}
RUN_KEYS_V1 = {
    "schema_version", "request_number", "status", "started_at", "updated_at",
    "finished_at", "request_sha256", "start_commit", "end_commit", "current_step",
    "steps", "call_counts", "model_attempts", "input_hashes", "output_hashes",
    "restored_files", "fallbacks", "errors", "resume",
}
RUN_KEYS_V2 = RUN_KEYS_V1 | {"request_revisions", "resume_count"}
RUN_KEYS_V3 = RUN_KEYS_V2 | {"model_calls", "events", "incidents", "lifecycle", "metrics"}


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
    return validate_run_data(run, filename_number, path.as_posix())


def validate_run_data(
    run: Any, filename_number: int, location: str = ".story-pipeline/runs"
) -> dict[str, Any]:
    """メモリ上の run 値をファイル読み込み時と同じ契約で検証する。"""
    run = _object(run, location)
    version = _integer(run.get("schema_version"), f"{location}#/schema_version")
    if version == 1:
        _keys(run, RUN_KEYS_V1, location)
        run = _migrate_v1(run)
    elif version == 2:
        _keys(run, RUN_KEYS_V2, location)
        run = _migrate_v2(run)
    elif version == 3:
        _keys(run, RUN_KEYS_V3, location)
    else:
        _fail("schema_version は 1、2、3 のいずれかである必要があります", f"{location}#/schema_version")
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
    if set(counts) not in (
        {"generation", "review", "revision", "summary"},
        {"generation", "review", "revision", "knowledge", "summary"},
    ):
        _fail("call_counts のキーが不正です", f"{location}#/call_counts")
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
    _bounded_integer(run["resume_count"], f"{location}#/resume_count", 0, 2**63 - 1)
    _validate_request_revisions(run, location)
    _validate_observability(run, location)
    return run


def _migrate_v1(run: dict[str, Any]) -> dict[str, Any]:
    """v1 の初回入力境界を失わず v3 へ補完する。"""
    migrated = dict(run)
    migrated["schema_version"] = 2
    migrated["resume_count"] = 0
    migrated["request_revisions"] = [
        {
            "sha256": run["request_sha256"],
            "input_commit": run["start_commit"],
            "accepted_at": run["started_at"],
            "applies_from_step": "interpret_request",
        }
    ]
    return _migrate_v2(migrated)


def _migrate_v2(run: dict[str, Any]) -> dict[str, Any]:
    """v2 の記録を観測値未取得の v3 として読み込む。"""
    migrated = dict(run)
    migrated["schema_version"] = 3
    migrated["model_calls"] = []
    migrated["events"] = []
    migrated["incidents"] = []
    state = "finalizing" if run["status"] != "running" else "executing"
    migrated["lifecycle"] = {
        "state": state,
        "history": [{"state": state, "occurred_at": run["updated_at"]}],
    }
    migrated["metrics"] = _empty_metrics()
    return migrated


def _empty_metrics() -> dict[str, Any]:
    return {
        "logical_calls": 0,
        "transport_attempts": 0,
        "retry_wait_ms": 0,
        "elapsed_ms": 0,
        "usage": {key: None for key in (
            "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
            "cost_usd",
        )},
    }


def _validate_observability(run: dict[str, Any], location: str) -> None:
    calls = _array(run["model_calls"], f"{location}#/model_calls")
    for index, value in enumerate(calls):
        item_location = f"{location}#/model_calls/{index}"
        item = _object(value, item_location)
        _keys(item, {
            "category", "role", "model_reference", "api_model", "started_at", "finished_at",
            "elapsed_ms", "result", "transport_attempts", "usage", "fallback_count", "truncated",
            "request_revision", "resume_count",
        }, item_location)
        for key in ("category", "role", "model_reference", "api_model", "result"):
            _string(item[key], f"{item_location}/{key}")
        _timestamp(item["started_at"], f"{item_location}/started_at")
        _timestamp(item["finished_at"], f"{item_location}/finished_at")
        for key in ("elapsed_ms", "fallback_count", "request_revision", "resume_count"):
            _bounded_integer(item[key], f"{item_location}/{key}", 0, 2**63 - 1)
        if not isinstance(item["truncated"], bool):
            _fail("boolean である必要があります", f"{item_location}/truncated")
        _validate_usage(item["usage"], f"{item_location}/usage")
        for attempt_index, attempt_value in enumerate(_array(item["transport_attempts"], f"{item_location}/transport_attempts")):
            _validate_transport_attempt(attempt_value, f"{item_location}/transport_attempts/{attempt_index}")
    for index, value in enumerate(_array(run["events"], f"{location}#/events")):
        item_location = f"{location}#/events/{index}"
        item = _object(value, item_location)
        _keys(item, {"kind", "step", "occurred_at", "details"}, item_location)
        _string(item["kind"], f"{item_location}/kind")
        _string(item["step"], f"{item_location}/step")
        _timestamp(item["occurred_at"], f"{item_location}/occurred_at")
        _object(item["details"], f"{item_location}/details")
    for index, value in enumerate(_array(run["incidents"], f"{location}#/incidents")):
        item_location = f"{location}#/incidents/{index}"
        item = _object(value, item_location)
        _keys(item, {"incident_id", "component", "exception_class", "step", "lifecycle", "retryable", "occurred_at"}, item_location)
        for key in ("incident_id", "component", "exception_class", "step", "lifecycle"):
            _string(item[key], f"{item_location}/{key}")
        if not isinstance(item["retryable"], bool):
            _fail("boolean である必要があります", f"{item_location}/retryable")
        _timestamp(item["occurred_at"], f"{item_location}/occurred_at")
    lifecycle = _object(run["lifecycle"], f"{location}#/lifecycle")
    _keys(lifecycle, {"state", "history"}, f"{location}#/lifecycle")
    _enum(lifecycle["state"], {"starting", "executing", "finalizing", "committed"}, f"{location}#/lifecycle/state")
    for index, value in enumerate(_array(lifecycle["history"], f"{location}#/lifecycle/history")):
        item = _object(value, f"{location}#/lifecycle/history/{index}")
        _keys(item, {"state", "occurred_at"}, f"{location}#/lifecycle/history/{index}")
        _enum(item["state"], {"starting", "executing", "finalizing", "committed"}, f"{location}#/lifecycle/history/{index}/state")
        _timestamp(item["occurred_at"], f"{location}#/lifecycle/history/{index}/occurred_at")
    metrics = _object(run["metrics"], f"{location}#/metrics")
    _keys(metrics, {"logical_calls", "transport_attempts", "retry_wait_ms", "elapsed_ms", "usage"}, f"{location}#/metrics")
    expected = {
        "logical_calls": len(calls),
        "transport_attempts": sum(len(item["transport_attempts"]) for item in calls),
        "retry_wait_ms": sum(attempt["wait_ms"] for item in calls for attempt in item["transport_attempts"]),
        "elapsed_ms": sum(item["elapsed_ms"] for item in calls),
    }
    for key, value in expected.items():
        if _bounded_integer(metrics[key], f"{location}#/metrics/{key}", 0, 2**63 - 1) != value:
            _fail("詳細記録と集計値が一致しません", f"{location}#/metrics/{key}")
    _validate_usage(metrics["usage"], f"{location}#/metrics/usage")
    for key in (
        "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
        "cost_usd",
    ):
        values = [item["usage"][key] for item in calls if item["usage"] is not None and item["usage"][key] is not None]
        expected_usage = sum(values) if values else None
        if metrics["usage"][key] != expected_usage:
            _fail("詳細記録と usage 集計が一致しません", f"{location}#/metrics/usage/{key}")


def _validate_transport_attempt(value: Any, location: str) -> None:
    item = _object(value, location)
    _keys(item, {"model_reference", "api_model", "attempt", "maximum_attempts", "started_at", "finished_at", "elapsed_ms", "result", "failure_kind", "wait_ms"}, location)
    for key in ("model_reference", "api_model", "result"):
        _string(item[key], f"{location}/{key}")
    for key in ("attempt", "maximum_attempts"):
        _bounded_integer(item[key], f"{location}/{key}", 1, 2**63 - 1)
    for key in ("elapsed_ms", "wait_ms"):
        _bounded_integer(item[key], f"{location}/{key}", 0, 2**63 - 1)
    _timestamp(item["started_at"], f"{location}/started_at")
    _timestamp(item["finished_at"], f"{location}/finished_at")
    if item["failure_kind"] is not None:
        _string(item["failure_kind"], f"{location}/failure_kind")


def _validate_usage(value: Any, location: str) -> None:
    if value is None:
        return
    usage = _object(value, location)
    keys = {
        "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
        "cost_usd",
    }
    _keys(usage, keys, location)
    for key in keys:
        if usage[key] is not None and key == "cost_usd":
            number = usage[key]
            if (
                not isinstance(number, (int, float)) or isinstance(number, bool)
                or not math.isfinite(float(number)) or number < 0
            ):
                _fail("非負の有限数である必要があります", f"{location}/{key}")
        elif usage[key] is not None:
            _bounded_integer(usage[key], f"{location}/{key}", 0, 2**63 - 1)


def _validate_request_revisions(run: dict[str, Any], location: str) -> None:
    revisions = _array(run["request_revisions"], f"{location}#/request_revisions")
    if not revisions:
        _fail("少なくとも初回要求の revision が必要です", f"{location}#/request_revisions")
    for index, value in enumerate(revisions):
        item_location = f"{location}#/request_revisions/{index}"
        revision = _object(value, item_location)
        _keys(
            revision,
            {"sha256", "input_commit", "accepted_at", "applies_from_step"},
            item_location,
        )
        _hash(revision["sha256"], f"{item_location}/sha256")
        _pattern_string(revision["input_commit"], COMMIT, f"{item_location}/input_commit")
        _timestamp(revision["accepted_at"], f"{item_location}/accepted_at")
        if not _string(revision["applies_from_step"], f"{item_location}/applies_from_step"):
            _fail("空でない工程名が必要です", f"{item_location}/applies_from_step")
    if revisions[-1]["sha256"] != run["request_sha256"]:
        _fail(
            "最新 revision の SHA-256 が request_sha256 と一致しません",
            f"{location}#/request_sha256",
        )


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
    for relative, expected in run["input_hashes"].items():
        if relative not in run["output_hashes"]:
            _check_hash(root, relative, expected, "RECORDED_HASH", collector, run_location)
    for relative, expected in run["output_hashes"].items():
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
