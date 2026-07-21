"""利用者へ安全に提示できる共通エラー。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StoryPipelineError(Exception):
    """終了コードと復旧案を伴う、捕捉済みの処理エラー。"""

    reason: str
    location: str
    action: str
    exit_code: int

    def __str__(self) -> str:
        return self.reason

