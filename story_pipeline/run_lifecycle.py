"""要求実行記録の生成と状態遷移。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal


RunStatus = Literal["completed", "failed", "awaiting_human"]
StepStatus = Literal["completed", "failed", "skipped"]
CALL_CATEGORIES = {"generation", "review", "revision", "knowledge", "summary"}
LIFECYCLE_STATES = {"starting", "executing", "finalizing", "committed"}


def utc_timestamp() -> str:
    """現在時刻を秒精度の UTC RFC 3339 形式で返す。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def create_run_record(
    request_number: int,
    request_sha256: str,
    start_commit: str,
    *,
    restored_files: tuple[str, ...] = (),
    now: str | None = None,
) -> dict[str, Any]:
    """開始時コミット後に保存する新規 running 記録を作る。"""
    if not 0 <= request_number <= 9999:
        raise ValueError("request_number は 0..9999 である必要があります")
    _require_hash(request_sha256)
    _require_commit(start_commit)
    timestamp = now or utc_timestamp()
    return {
        "schema_version": 3,
        "request_number": request_number,
        "status": "running",
        "started_at": timestamp,
        "updated_at": timestamp,
        "finished_at": None,
        "request_sha256": request_sha256,
        "start_commit": start_commit,
        "end_commit": None,
        "current_step": "interpret_request",
        "steps": [],
        "call_counts": {
            "generation": 0,
            "review": 0,
            "revision": 0,
            "knowledge": 0,
            "summary": 0,
        },
        "model_attempts": [],
        "input_hashes": {},
        "output_hashes": {},
        "restored_files": list(restored_files),
        "fallbacks": [],
        "errors": [],
        "resume": None,
        "request_revisions": [
            {
                "sha256": request_sha256,
                "input_commit": start_commit,
                "accepted_at": timestamp,
                "applies_from_step": "interpret_request",
            }
        ],
        "resume_count": 0,
        "model_calls": [],
        "events": [],
        "incidents": [],
        "lifecycle": {
            "state": "executing",
            "history": [
                {"state": "starting", "occurred_at": timestamp},
                {"state": "executing", "occurred_at": timestamp},
            ],
        },
        "metrics": _empty_metrics(),
    }


def resume_run_record(
    run: dict[str, Any],
    *,
    step: str,
    reason: str,
    request_sha256: str | None = None,
    input_commit: str | None = None,
    applies_from_step: str = "interpret_request",
    now: str | None = None,
) -> dict[str, Any]:
    """failed 記録を running に戻し、最初の開始時刻と累積値を保つ。"""
    if run.get("status") != "failed":
        raise ValueError("failed の実行記録だけを再開できます")
    updated = deepcopy(run)
    updated["call_counts"].setdefault("knowledge", 0)
    if updated.get("schema_version") == 1:
        updated["request_revisions"] = [{
            "sha256": updated["request_sha256"],
            "input_commit": updated["start_commit"],
            "accepted_at": updated["started_at"],
            "applies_from_step": "interpret_request",
        }]
        updated["resume_count"] = 0
    if updated.get("schema_version", 1) < 3:
        updated["schema_version"] = 3
        updated["model_calls"] = []
        updated["events"] = []
        updated["incidents"] = []
        updated["lifecycle"] = {
            "state": "executing",
            "history": [{"state": "executing", "occurred_at": now or utc_timestamp()}],
        }
        updated["metrics"] = _empty_metrics()
    timestamp = now or utc_timestamp()
    if request_sha256 is not None and request_sha256 != updated["request_sha256"]:
        _require_hash(request_sha256)
        if input_commit is None:
            raise ValueError("要求改訂には入力 commit が必要です")
        _require_commit(input_commit)
        if not applies_from_step:
            raise ValueError("要求改訂には適用開始工程が必要です")
        updated["request_sha256"] = request_sha256
        updated["request_revisions"].append({
            "sha256": request_sha256,
            "input_commit": input_commit,
            "accepted_at": timestamp,
            "applies_from_step": applies_from_step,
        })
    updated["status"] = "running"
    updated["finished_at"] = None
    updated["updated_at"] = timestamp
    updated["current_step"] = step
    updated["resume"] = {"step": step, "reason": reason}
    updated["resume_count"] += 1
    updated["lifecycle"]["state"] = "executing"
    updated["lifecycle"]["history"].append({"state": "executing", "occurred_at": timestamp})
    return updated


def start_step(
    run: dict[str, Any],
    identifier: str,
    *,
    input_hashes: dict[str, str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """新しい工程を running として開始する。"""
    _require_running(run)
    if not identifier or any(item["id"] == identifier for item in run["steps"]):
        raise ValueError("工程 ID は空でない一意な値である必要があります")
    hashes = dict(input_hashes or {})
    _require_hashes(hashes)
    timestamp = now or utc_timestamp()
    updated = deepcopy(run)
    updated["steps"].append(
        {
            "id": identifier,
            "status": "running",
            "started_at": timestamp,
            "finished_at": None,
            "input_hashes": hashes,
            "output_hashes": {},
            "result": None,
        }
    )
    updated["current_step"] = identifier
    updated["updated_at"] = timestamp
    updated["input_hashes"].update(hashes)
    return updated


def finish_step(
    run: dict[str, Any],
    identifier: str,
    status: StepStatus,
    *,
    output_hashes: dict[str, str] | None = None,
    result: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """running 工程を完了、失敗、または省略として確定する。"""
    if status not in {"completed", "failed", "skipped"}:
        raise ValueError("工程の終了 status が不正です")
    hashes = dict(output_hashes or {})
    _require_hashes(hashes)
    updated = deepcopy(run)
    matching = [item for item in updated["steps"] if item["id"] == identifier]
    if len(matching) != 1 or matching[0]["status"] != "running":
        raise ValueError("running の対象工程が一意に存在しません")
    timestamp = now or utc_timestamp()
    step = matching[0]
    step["status"] = status
    step["finished_at"] = timestamp
    step["output_hashes"] = hashes
    step["result"] = result
    updated["updated_at"] = timestamp
    updated["output_hashes"].update(hashes)
    return updated


def record_model_attempt(
    run: dict[str, Any],
    *,
    category: str,
    role: str,
    model_reference: str,
    api_model: str,
    started_at: str,
    finished_at: str,
    result: str,
    attempts: int = 1,
    token_count: int | None = None,
    fallbacks: tuple[dict[str, str], ...] = (),
    transport_attempts: tuple[dict[str, Any], ...] = (),
    usage: dict[str, int | None] | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """秘密や応答本文を含めず、論理 LLM 呼び出しを累積記録する。"""
    if category not in CALL_CATEGORIES:
        raise ValueError("呼び出し category が不正です")
    if attempts < 1 or token_count is not None and token_count < 0:
        raise ValueError("呼び出し回数または token_count が不正です")
    updated = deepcopy(run)
    updated["call_counts"][category] += 1
    updated["model_attempts"].append(
        {
            "category": category,
            "role": role,
            "model_reference": model_reference,
            "api_model": api_model,
            "started_at": started_at,
            "finished_at": finished_at,
            "result": result,
            "attempts": attempts,
            "token_count": token_count,
            "request_revision": len(updated["request_revisions"]) - 1,
        }
    )
    updated["fallbacks"].extend(deepcopy(list(fallbacks)))
    elapsed_ms = sum(item["elapsed_ms"] for item in transport_attempts)
    updated["model_calls"].append(
        {
            "category": category,
            "role": role,
            "model_reference": model_reference,
            "api_model": api_model,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_ms": elapsed_ms,
            "result": result,
            "transport_attempts": deepcopy(list(transport_attempts)),
            "usage": deepcopy(usage),
            "fallback_count": len(fallbacks),
            "truncated": truncated,
            "request_revision": len(updated["request_revisions"]) - 1,
            "resume_count": updated["resume_count"],
        }
    )
    updated["metrics"] = _calculate_metrics(updated["model_calls"])
    updated["updated_at"] = finished_at
    return updated


def record_event(
    run: dict[str, Any],
    *,
    kind: str,
    step: str,
    details: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """秘密値や本文を含めない構造化進捗イベントを追記する。"""
    _require_running(run)
    if not kind or not step:
        raise ValueError("event には kind と step が必要です")
    updated = deepcopy(run)
    timestamp = now or utc_timestamp()
    updated["events"].append({
        "kind": kind,
        "step": step,
        "occurred_at": timestamp,
        "details": deepcopy(details or {}),
    })
    updated["updated_at"] = timestamp
    return updated


def transition_lifecycle(
    run: dict[str, Any], state: str, *, now: str | None = None
) -> dict[str, Any]:
    """run 終了境界の lifecycle を単調に進める。"""
    if state not in LIFECYCLE_STATES:
        raise ValueError("lifecycle state が不正です")
    order = ("starting", "executing", "finalizing", "committed")
    current = run["lifecycle"]["state"]
    if order.index(state) < order.index(current):
        raise ValueError("lifecycle を逆行できません")
    if state == current:
        return deepcopy(run)
    updated = deepcopy(run)
    timestamp = now or utc_timestamp()
    updated["lifecycle"]["state"] = state
    updated["lifecycle"]["history"].append({"state": state, "occurred_at": timestamp})
    updated["updated_at"] = timestamp
    return updated


def record_incident(
    run: dict[str, Any],
    *,
    incident_id: str,
    component: str,
    exception_class: str,
    step: str,
    retryable: bool,
    now: str | None = None,
) -> dict[str, Any]:
    """例外 message や traceback を保存せず障害識別情報を追記する。"""
    if not incident_id or not component or not exception_class or not step:
        raise ValueError("incident の必須値が不足しています")
    updated = deepcopy(run)
    timestamp = now or utc_timestamp()
    updated["incidents"].append({
        "incident_id": incident_id,
        "component": component,
        "exception_class": exception_class,
        "step": step,
        "lifecycle": updated["lifecycle"]["state"],
        "retryable": retryable,
        "occurred_at": timestamp,
    })
    updated["updated_at"] = timestamp
    return updated


def finalize_run_record(
    run: dict[str, Any],
    status: RunStatus,
    *,
    resume_step: str | None = None,
    resume_reason: str | None = None,
    error: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """running 記録を終了状態へ遷移させる。"""
    _require_running(run)
    if status not in {"completed", "failed", "awaiting_human"}:
        raise ValueError("実行の終了 status が不正です")
    if status == "failed" and (not resume_step or not resume_reason):
        raise ValueError("failed には再開位置と理由が必要です")
    if any(step["status"] == "running" for step in run["steps"]):
        raise ValueError("running の工程を残したまま実行を終了できません")
    timestamp = now or utc_timestamp()
    updated = deepcopy(run)
    updated["status"] = status
    updated["updated_at"] = timestamp
    updated["finished_at"] = timestamp
    updated["resume"] = (
        {"step": resume_step, "reason": resume_reason}
        if status == "failed"
        else None
    )
    if error is not None:
        required = {"step", "category", "message", "retryable"}
        if set(error) != required:
            raise ValueError("error のキーが不正です")
        updated["errors"].append({**deepcopy(error), "occurred_at": timestamp})
    return updated


def _require_running(run: dict[str, Any]) -> None:
    if run.get("status") != "running":
        raise ValueError("running の実行記録が必要です")


def _empty_metrics() -> dict[str, Any]:
    return {
        "logical_calls": 0,
        "transport_attempts": 0,
        "retry_wait_ms": 0,
        "elapsed_ms": 0,
        "usage": {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "cached_tokens": None,
            "reasoning_tokens": None,
            "cost_usd": None,
        },
    }


def _calculate_metrics(calls: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = _empty_metrics()
    metrics["logical_calls"] = len(calls)
    metrics["transport_attempts"] = sum(len(item["transport_attempts"]) for item in calls)
    metrics["retry_wait_ms"] = sum(
        attempt["wait_ms"] for item in calls for attempt in item["transport_attempts"]
    )
    metrics["elapsed_ms"] = sum(item["elapsed_ms"] for item in calls)
    for key in metrics["usage"]:
        values = [item["usage"][key] for item in calls if item["usage"] is not None and item["usage"][key] is not None]
        metrics["usage"][key] = sum(values) if values else None
    return metrics


def _require_hashes(hashes: dict[str, str]) -> None:
    for value in hashes.values():
        _require_hash(value)


def _require_hash(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("SHA-256 は小文字64桁の16進数である必要があります")


def _require_commit(value: str) -> None:
    if len(value) not in {40, 64} or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("Git object ID は完全な16進数である必要があります")
