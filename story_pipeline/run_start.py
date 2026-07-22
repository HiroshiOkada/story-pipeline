"""`run` の副作用前検査と開始時永続化。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, TextIO

from story_pipeline.config import load_config
from story_pipeline.environment import load_environment
from story_pipeline.execution_store import persist_new_execution, persist_resumed_execution
from story_pipeline.git_safety import (
    GitPreflight,
    commit_start_inputs,
    current_commit,
    inspect_run_preconditions,
    restore_managed_files,
    validate_run_repository,
)
from story_pipeline.git_validation import classify_path
from story_pipeline.llm_client import LLMClient
from story_pipeline.llm_connection import check_initial_connections
from story_pipeline.persistence import sha256_file
from story_pipeline.project import find_project_root
from story_pipeline.request_selection import SelectedRequest, select_request
from story_pipeline.run_lifecycle import create_run_record, resume_run_record
from story_pipeline.run_lock import RunLock
from story_pipeline.runs import validate_run_data
from story_pipeline.state import load_state


@dataclass(slots=True)
class RunStart:
    root: Path
    config: dict[str, Any]
    state: dict[str, Any]
    request: SelectedRequest
    run: dict[str, Any]
    client: LLMClient
    lock: RunLock


def prepare_run(
    *,
    output: TextIO,
    root: Path | None = None,
    client: LLMClient | None = None,
) -> RunStart | None:
    """仕様順に前提を検査し、running 記録を作成または再開する。"""
    project_root = find_project_root(root)
    config = load_config(project_root)
    state = load_state(project_root)
    validate_run_repository(project_root)
    lock = RunLock.acquire(project_root)
    try:
        preflight = inspect_run_preconditions(project_root, config)
        request = select_request(project_root, state)
        if request is None:
            lock.release()
            return None
        lock.update_request(request.number)
        environment = load_environment(config)
        llm = client or LLMClient(config, environment)
        check_initial_connections(config, environment, _initial_models(config), client=llm)
        restored = restore_managed_files(project_root, preflight, output)
        if request.mode == "resume":
            run = _load_resume_run(project_root, request.number)
            request_hash = sha256_file(project_root / request.relative_path)
            changed_paths = _changed_start_paths(preflight, request.relative_path)
            input_commit = _record_resume_inputs(
                project_root, request.number, changed_paths, request_hash != run["request_sha256"]
            )
            changed_request = request_hash != run["request_sha256"]
            run = resume_run_record(
                run,
                step="interpret_request" if changed_request else run["resume"]["step"],
                reason=("要求改訂を受け入れて再解釈" if changed_request else run["resume"]["reason"]),
                request_sha256=request_hash,
                input_commit=input_commit if changed_request else None,
            )
            state = persist_resumed_execution(project_root, state, run)
        else:
            paths = _changed_start_paths(preflight, request.relative_path)
            commit = commit_start_inputs(project_root, request.number, paths)
            start_commit = commit or current_commit(project_root)
            run = create_run_record(
                request.number,
                sha256_file(project_root / request.relative_path),
                start_commit,
                restored_files=restored,
            )
            state = persist_new_execution(project_root, state, run)
        return RunStart(project_root, config, state, request, run, llm, lock)
    except BaseException:
        lock.release()
        raise


def _initial_models(config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(config["roles"][role][0] for role in sorted(config["roles"])))


def _changed_start_paths(preflight: GitPreflight, request_path: str) -> tuple[str, ...]:
    allowed = {request_path, "story-pipeline-config.jsonc"}
    return tuple(
        sorted(
            {
                entry.normalized_path()
                for entry in preflight.entries
                if not entry.rename_origin
                and entry.normalized_path() in allowed
                and entry.kind != "ignored"
                and classify_path(entry.normalized_path(), set(preflight.configured_dotenv)) == "human"
            }
        )
    )


def _record_resume_inputs(
    root: Path,
    request_number: int,
    paths: tuple[str, ...],
    request_changed: bool,
) -> str:
    """再開入力の差分を限定 commit し、改訂境界となる commit を返す。"""
    commit = commit_start_inputs(root, request_number, paths)
    boundary = commit or current_commit(root)
    if request_changed and not boundary:
        raise ValueError("要求改訂の入力 commit を特定できません")
    return boundary


def _load_resume_run(root: Path, number: int) -> dict[str, Any]:
    path = root / ".story-pipeline" / "runs" / f"{number:04d}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    run = validate_run_data(value, number, path.as_posix())
    if run["status"] != "failed" or run["resume"] is None:
        raise ValueError("再開対象 run に再開情報がありません")
    return run
