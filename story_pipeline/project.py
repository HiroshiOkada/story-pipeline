"""Story Pipeline 作品ルートの特定。"""

from __future__ import annotations

from pathlib import Path

from story_pipeline.errors import StoryPipelineError


CONFIG_FILENAME = "story-pipeline-config.jsonc"
EXIT_NOT_INITIALIZED = 3


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
        if (directory / CONFIG_FILENAME).is_file():
            return directory
    raise StoryPipelineError(
        "Story Pipeline の作品ルートを特定できません。",
        str(resolved),
        "story-pipeline init で初期化するか、作品ディレクトリへ移動してください。",
        EXIT_NOT_INITIALIZED,
    )
