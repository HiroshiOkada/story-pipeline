"""Story Pipeline のコマンドラインインターフェース。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import subprocess
import sys

from story_pipeline import __version__
from story_pipeline.config import load_config
from story_pipeline.errors import StoryPipelineError
from story_pipeline.environment import validate_environment
from story_pipeline.git_validation import validate_git
from story_pipeline.project import find_project_root
from story_pipeline.project_validation import validate_project_files
from story_pipeline.run_command import run_command
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state
from story_pipeline.status import determine_next_action, inspect_status
from story_pipeline.validation import IssueCollector


EXIT_CONFIG = 4
EXIT_GIT = 5
EXIT_IO = 9


def _init_path(value: str) -> str:
    if value == "-":
        raise argparse.ArgumentTypeError("PATH に '-' は指定できません")
    return value


def build_parser() -> argparse.ArgumentParser:
    """CLI の引数パーサーを構築する。"""
    parser = argparse.ArgumentParser(
        prog="story-pipeline",
        description="AI と人間が協調して小説を制作するパイプライン",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"story-pipeline {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser(
        "init", help="新しい Story Pipeline プロジェクトを初期化する"
    )
    init_parser.add_argument(
        "path", nargs="?", default=".", type=_init_path, metavar="PATH"
    )

    subparsers.add_parser("run", help="次の未処理要求を実行する")
    subparsers.add_parser("status", help="作品の現在状態を表示する")
    subparsers.add_parser("validate", help="作品と設定の整合性を検証する")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI を実行し、プロセス終了コードを返す。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            parser.print_help()
        elif args.command == "init":
            return _init_project(args.path)
        elif args.command == "status":
            return _show_status()
        elif args.command == "validate":
            return _validate_project()
        elif args.command == "run":
            return run_command(output=sys.stdout, error_output=sys.stderr)
        return 0
    except StoryPipelineError as error:
        return _error(error.reason, error.location, error.action, error.exit_code)
    except Exception:
        return _error(
            "予期しない内部エラーが発生しました。",
            "story-pipeline",
            "入力ファイルを変更せず、問題を報告してください。",
            EXIT_IO,
        )


def _error(reason: str, location: str, action: str, code: int) -> int:
    print(f"Error: {reason}", file=sys.stderr)
    print(f"Location: {location}", file=sys.stderr)
    print(f"Action: {action}", file=sys.stderr)
    return code


def _init_project(raw_path: str) -> int:
    target = Path(raw_path)
    try:
        root = target.resolve(strict=True)
    except OSError:
        return _error(
            "対象パスが存在しません。", raw_path, "既存の空ディレクトリを指定してください。", EXIT_CONFIG
        )

    if not root.is_dir():
        return _error(
            "対象パスはディレクトリではありません。",
            str(root),
            "既存の空ディレクトリを指定してください。",
            EXIT_CONFIG,
        )

    config_path = root / "story-pipeline-config.jsonc"
    if config_path.exists():
        return _error(
            "すでに初期化されています。",
            str(config_path),
            "既存のプロジェクトをそのまま使用してください。",
            EXIT_CONFIG,
        )

    try:
        entries = list(root.iterdir())
    except OSError:
        return _error(
            "対象ディレクトリを読み取れません。",
            str(root),
            "ディレクトリの権限を確認してください。",
            EXIT_IO,
        )
    if any(entry.name != ".git" for entry in entries):
        return _error(
            "初期化されていない空でないディレクトリです。",
            str(root),
            "空のディレクトリを指定してください。",
            EXIT_CONFIG,
        )

    try:
        create_scaffold(root)
    except OSError:
        return _error(
            "scaffold を作成できませんでした。",
            str(root),
            "書き込み権限と空き容量を確認してください。",
            EXIT_IO,
        )

    if not _is_git_repository(root):
        try:
            subprocess.run(
                ["git", "init", str(root)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            return _error(
                "Git リポジトリを初期化できませんでした。",
                str(root),
                "Git の導入状態と権限を確認してください。",
                EXIT_GIT,
            )

    print(f"Initialized Story Pipeline project: {root}")
    print("Next request: requests/0000.md")
    print("Run: story-pipeline run")
    return 0


def _is_git_repository(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def _show_status() -> int:
    root = find_project_root()
    load_config(root)
    state = load_state(root)
    snapshot = inspect_status(root, state)
    last_request = _request_label(state["last_request"], snapshot.last_request_status)
    active_request = _request_label(state["active_request"])
    current_chapter = _number_label(state["current_chapter"])

    print(f"Root: {root}")
    print(f"Phase: {state['phase']}")
    print(f"Last request: {last_request}")
    print(f"Active request: {active_request}")
    print(f"Current chapter: {current_chapter}")
    print(f"Next episode: {state['next_episode']:04d}")
    print(f"Completed chapters: {len(state['completed_chapters'])}")
    print(f"Completed episodes: {len(state['completed_episodes'])}")
    print(f"Pending reviews: {len(state['pending_reviews'])}")
    print(f"Pending decisions: {len(state['pending_decisions'])}")
    if snapshot.lock_info is not None:
        print(f"Lock: {snapshot.lock_info}")
    print(f"Next action: {determine_next_action(root, state)}")
    for warning in snapshot.warnings:
        print(f"Warning: {warning.code} {warning.message}", file=sys.stderr)
    return 0


def _request_label(number: int | None, status: str | None = None) -> str:
    if number is None:
        return "none"
    suffix = "" if status is None else f" ({status})"
    return f"{number:04d}{suffix}"


def _number_label(number: int | None) -> str:
    return "none" if number is None else f"{number:04d}"


def _validate_project() -> int:
    root = find_project_root()
    collector = IssueCollector()
    context = validate_project_files(root, collector)
    validate_git(root, collector, context.config)
    validate_environment(context.config, collector)
    for issue in collector.issues:
        print(issue.format())
    if collector.error_count:
        print(
            f"Validation failed: {collector.error_count} error(s), "
            f"{collector.warning_count} warning(s)."
        )
        return EXIT_CONFIG
    if collector.warning_count:
        print(f"Validation passed with {collector.warning_count} warning(s).")
    else:
        print("Validation passed.")
    return 0
