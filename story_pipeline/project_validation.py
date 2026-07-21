"""設定、状態、要求、成果物の横断検証。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from story_pipeline.config import load_config
from story_pipeline.runs import validate_runs
from story_pipeline.state import load_state
from story_pipeline.status import inspect_status
from story_pipeline.validation import IssueCollector


REQUEST_FILE = re.compile(r"^([0-9]{4})(_agent)?\.md$")
NUMBERED_FILE = re.compile(r"^[0-9]{4}\.md$")
TOP_LEVEL_ARTIFACTS = {"concept.md", "world.md", "characters.md", "plot.md", "style.md", "canon.md"}
NUMBERED_DIRECTORIES = {"chapters", "episode_plans", "episodes"}


@dataclass(frozen=True, slots=True)
class ValidationContext:
    config: dict[str, Any] | None
    state: dict[str, Any] | None
    runs: dict[int, dict[str, Any]]


def validate_project_files(root: Path, collector: IssueCollector) -> ValidationContext:
    """Git と環境以外のプロジェクトファイルを検証する。"""
    config_value = collector.capture(
        "CONFIG_INVALID", lambda: load_config(root), message="設定を検証できません"
    )
    state_value = collector.capture(
        "STATE_INVALID", lambda: load_state(root), message="状態を検証できません"
    )
    config = config_value if isinstance(config_value, dict) else None
    state = state_value if isinstance(state_value, dict) else None
    _validate_managed_paths(root, collector)
    runs = validate_runs(root, state, collector)
    if state is not None:
        _validate_status_consistency(root, state, collector)
    _validate_request_correspondence(root, state, runs, collector)
    return ValidationContext(config, state, runs)


def _validate_managed_paths(root: Path, collector: IssueCollector) -> None:
    for directory_name in ("requests", *sorted(NUMBERED_DIRECTORIES), ".story-pipeline"):
        path = root / directory_name
        if not _safe_path(root, path, expected="directory"):
            collector.error("MANAGED_DIRECTORY_INVALID", "管理対象が安全なディレクトリではありません", directory_name)

    for name in TOP_LEVEL_ARTIFACTS:
        path = root / name
        if (path.exists() or path.is_symlink()) and not _safe_path(root, path, expected="file"):
            collector.error("MANAGED_PATH_INVALID", "管理対象が安全な通常ファイルではありません", name)

    for directory_name in NUMBERED_DIRECTORIES:
        directory = root / directory_name
        if not _safe_path(root, directory, expected="directory"):
            continue
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            if not NUMBERED_FILE.fullmatch(entry.name):
                collector.warning("UNKNOWN_MANAGED_DIRECTORY_FILE", "管理ディレクトリ内の命名規則に一致しません", relative)
            elif not _safe_path(root, entry, expected="file"):
                collector.error("MANAGED_PATH_INVALID", "管理対象が安全な通常ファイルではありません", relative)

    requests = root / "requests"
    if _safe_path(root, requests, expected="directory"):
        try:
            entries = list(requests.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            if not REQUEST_FILE.fullmatch(entry.name):
                collector.warning("UNKNOWN_REQUEST_FILE", "要求ファイルの命名規則に一致しません", relative)
            elif not _safe_path(root, entry, expected="file"):
                collector.error("REQUEST_PATH_INVALID", "要求または報告が安全な通常ファイルではありません", relative)


def _validate_status_consistency(
    root: Path, state: dict[str, Any], collector: IssueCollector
) -> None:
    snapshot = inspect_status(root, state)
    ignored = {"LOCK_INVALID", "RUN_FILE_MISSING", "RUN_STATUS_INVALID"}
    for warning in snapshot.warnings:
        if warning.code in ignored:
            if warning.code == "LOCK_INVALID":
                collector.error(warning.code, warning.message, ".story-pipeline/run.lock")
            continue
        collector.error(f"STATE_{warning.code}", warning.message)


def _validate_request_correspondence(
    root: Path,
    state: dict[str, Any] | None,
    runs: dict[int, dict[str, Any]],
    collector: IssueCollector,
) -> None:
    requests, reports = _request_numbers(root)
    for number in sorted(reports - requests):
        collector.error("REPORT_REQUEST_MISSING", "処理報告に対応する要求がありません", f"requests/{number:04d}_agent.md")
    for number, run in sorted(runs.items()):
        if number not in requests:
            collector.error("RUN_REQUEST_MISSING", "実行記録に対応する要求がありません", f"requests/{number:04d}.md")
        if run["status"] != "running" and number not in reports:
            collector.error("RUN_REPORT_MISSING", "終了した実行記録に対応する処理報告がありません", f"requests/{number:04d}_agent.md")
    for number in sorted(reports):
        if number not in runs:
            collector.error("REPORT_RUN_MISSING", "処理報告に対応する実行記録がありません", f".story-pipeline/runs/{number:04d}.json")
    if state is not None and state["last_request"] is not None:
        last = state["last_request"]
        later_finished = [number for number, run in runs.items() if number > last and run["status"] != "running"]
        if later_finished:
            collector.error("STATE_LAST_REQUEST_ORDER", "last_request より新しい終了済み実行記録があります", ".story-pipeline/state.json#/last_request")


def _request_numbers(root: Path) -> tuple[set[int], set[int]]:
    requests: set[int] = set()
    reports: set[int] = set()
    directory = root / "requests"
    if not _safe_path(root, directory, expected="directory"):
        return requests, reports
    try:
        entries = directory.iterdir()
    except OSError:
        return requests, reports
    for entry in entries:
        match = REQUEST_FILE.fullmatch(entry.name)
        if match and _safe_path(root, entry, expected="file"):
            target = reports if match.group(2) else requests
            target.add(int(match.group(1)))
    return requests, reports


def _safe_path(root: Path, path: Path, *, expected: str) -> bool:
    try:
        target = path.resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError):
        return False
    return target.is_file() if expected == "file" else target.is_dir()
