"""終了時コミット後に作る次要求テンプレートの採番と安全な作成。"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from story_pipeline.errors import StoryPipelineError
from story_pipeline.persistence import atomic_create_text


NUMBERED_REQUEST = re.compile(r"^([0-9]{4})(?:_agent)?\.md$")
NEXT_REQUEST_TEMPLATE = """# 追加要求

<!-- 継続、修正、追加、再検討などの要求を記述してください。 -->
"""


def create_next_request(root: Path) -> str:
    """要求と報告の最大番号に1を加えた空テンプレートを作成する。"""
    directory = root / "requests"
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise OSError("通常ディレクトリではありません")
        entries = list(directory.iterdir())
    except OSError as error:
        raise _request_error("要求ディレクトリを安全に読み取れません", directory, 9) from error
    numbers: list[int] = []
    for entry in entries:
        match = NUMBERED_REQUEST.fullmatch(entry.name)
        if match is None:
            continue
        try:
            regular = stat.S_ISREG(os.lstat(entry).st_mode)
        except OSError:
            regular = False
        if not regular:
            raise _request_error("採番対象が安全な通常ファイルではありません", entry, 4)
        numbers.append(int(match.group(1)))
    maximum = max(numbers, default=-1)
    if maximum >= 9999:
        raise _request_error(
            "要求番号が上限 9999 に達しました",
            directory,
            8,
            "既存の要求を退避する方針を決めてください",
        )
    number = maximum + 1
    relative = f"requests/{number:04d}.md"
    atomic_create_text(root / relative, NEXT_REQUEST_TEMPLATE)
    return relative


def _request_error(
    reason: str,
    path: Path,
    exit_code: int,
    action: str = "requests ディレクトリの内容を確認してください",
) -> StoryPipelineError:
    return StoryPipelineError(reason, str(path), action, exit_code)
