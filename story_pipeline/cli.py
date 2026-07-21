"""Story Pipeline のコマンドラインインターフェース。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from story_pipeline import __version__


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
    init_parser.add_argument("path", nargs="?", default=".", metavar="PATH")

    subparsers.add_parser("run", help="次の未処理要求を実行する")
    subparsers.add_parser("status", help="作品の現在状態を表示する")
    subparsers.add_parser("validate", help="作品と設定の整合性を検証する")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI を実行し、プロセス終了コードを返す。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0
