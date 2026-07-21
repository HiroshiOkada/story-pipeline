"""Story Pipeline 作品ルートの特定。"""

from __future__ import annotations

import os
from pathlib import Path

from story_pipeline.errors import StoryPipelineError


CONFIG_FILENAME = "story-pipeline-config.jsonc"
EXIT_NOT_INITIALIZED = 3
EXIT_CONFIG = 4


def find_project_root(start: Path | None = None) -> Path:
    """開始位置から祖先をたどり、最も近い作品ルートを返す。"""
    candidate = Path.cwd() if start is None else start
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise StoryPipelineError(
            "開始位置が存在しないか読み取れません。",
            str(candidate),
            "初期化済みの作品ディレクトリへ移動してください。",
            EXIT_NOT_INITIALIZED,
        ) from error

    if not resolved.is_dir():
        resolved = resolved.parent
    for directory in (resolved, *resolved.parents):
        config_path = directory / CONFIG_FILENAME
        try:
            os.lstat(config_path)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise _invalid_config(config_path, "設定ファイルの種別を確認できません。") from error
        try:
            target = config_path.resolve(strict=True)
            target.relative_to(directory)
        except (OSError, ValueError) as error:
            raise _invalid_config(
                config_path, "設定ファイルが探索対象ディレクトリ外を指しています。"
            ) from error
        if not target.is_file():
            raise _invalid_config(config_path, "設定パスが通常ファイルではありません。")
        try:
            with target.open("rb") as stream:
                stream.read(1)
        except OSError as error:
            raise _invalid_config(config_path, "設定ファイルを読み取れません。") from error
        return directory
    raise StoryPipelineError(
        "Story Pipeline の作品ルートを特定できません。",
        str(resolved),
        "story-pipeline init で初期化するか、作品ディレクトリへ移動してください。",
        EXIT_NOT_INITIALIZED,
    )


def _invalid_config(path: Path, reason: str) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        str(path),
        "設定ファイルの種別、リンク先、読み取り権限を確認してください。",
        EXIT_CONFIG,
    )
