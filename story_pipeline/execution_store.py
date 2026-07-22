"""run と state の検証済み永続化および要求状態遷移。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from story_pipeline.persistence import atomic_write_json
from story_pipeline.run_lifecycle import utc_timestamp
from story_pipeline.runs import validate_run_data
from story_pipeline.state import validate_state_data


def persist_new_execution(
    root: Path,
    state: dict[str, Any],
    run: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """新規 run を先に保存し、その後 state.active_request を設定する。"""
    number = _request_number(run)
    path = _run_path(root, number)
    if path.exists() or path.is_symlink():
        raise ValueError(f"実行記録がすでに存在します: {path}")
    if state.get("active_request") is not None:
        raise ValueError("新規実行の開始時は active_request が null である必要があります")
    validate_run_data(run, number, path.as_posix())
    if run["status"] != "running":
        raise ValueError("新規実行記録は running である必要があります")
    updated_state = _state_with_request(state, number, active=True, now=now)
    _persist_pair(root, run, updated_state)
    return updated_state


def persist_resumed_execution(
    root: Path,
    state: dict[str, Any],
    run: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """既存 active request の running 復帰を run、state の順で保存する。"""
    number = _request_number(run)
    path = _run_path(root, number)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"再開対象の安全な実行記録がありません: {path}")
    if state.get("active_request") != number or run["status"] != "running":
        raise ValueError("active request と再開 run が一致しません")
    validate_run_data(run, number, path.as_posix())
    updated_state = _state_with_request(state, number, active=True, now=now)
    _persist_pair(root, run, updated_state)
    return updated_state


def persist_run_progress(root: Path, run: dict[str, Any]) -> None:
    """工程途中の検証済み run だけを保存する。"""
    number = _request_number(run)
    path = _run_path(root, number)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"更新対象の安全な実行記録がありません: {path}")
    validate_run_data(run, number, path.as_posix())
    atomic_write_json(path, run)


def persist_finished_execution(
    root: Path,
    state: dict[str, Any],
    run: dict[str, Any],
    *,
    state_updates: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """終了 run を先に、採用結果を反映した state を後に保存する。"""
    number = _request_number(run)
    path = _run_path(root, number)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"終了対象の安全な実行記録がありません: {path}")
    if run["status"] not in {"completed", "failed", "awaiting_human"}:
        raise ValueError("run が終了状態ではありません")
    if state.get("active_request") != number:
        raise ValueError("終了 run が active_request と一致しません")
    updated_state = deepcopy(state)
    protected = {"schema_version", "last_request", "active_request", "updated_at"}
    for key, value in (state_updates or {}).items():
        if key in protected or key not in updated_state:
            raise ValueError(f"state_updates で変更できないキーです: {key}")
        updated_state[key] = deepcopy(value)
    updated_state["last_request"] = number
    updated_state["active_request"] = number if run["status"] == "failed" else None
    updated_state["updated_at"] = now or utc_timestamp()
    validate_run_data(run, number, path.as_posix())
    validate_state_data(updated_state)
    _persist_pair(root, run, updated_state)
    return updated_state


def _persist_pair(root: Path, run: dict[str, Any], state: dict[str, Any]) -> None:
    atomic_write_json(_run_path(root, run["request_number"]), run)
    atomic_write_json(root / ".story-pipeline" / "state.json", state)


def _state_with_request(
    state: dict[str, Any], number: int, *, active: bool, now: str | None
) -> dict[str, Any]:
    updated = deepcopy(state)
    updated["active_request"] = number if active else None
    updated["updated_at"] = now or utc_timestamp()
    validate_state_data(updated)
    return updated


def _request_number(run: dict[str, Any]) -> int:
    number = run.get("request_number")
    if type(number) is not int or not 0 <= number <= 9999:
        raise ValueError("run.request_number が不正です")
    return number


def _run_path(root: Path, number: int) -> Path:
    return root / ".story-pipeline" / "runs" / f"{number:04d}.json"
