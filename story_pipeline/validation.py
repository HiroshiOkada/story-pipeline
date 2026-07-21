"""プロジェクト全体の検証問題を収集する基盤。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from story_pipeline.errors import StoryPipelineError


Severity = Literal["ERROR", "WARNING"]


@dataclass(frozen=True, slots=True, order=True)
class ValidationIssue:
    """機械判読可能なコードを持つ検証結果。"""

    severity: Severity
    code: str
    message: str
    location: str

    def format(self) -> str:
        suffix = "" if not self.location else f": {self.location}"
        return f"{self.severity} {self.code} {self.message}{suffix}"


class IssueCollector:
    """独立した検査の失敗後も、可能な検査を続行する。"""

    def __init__(self) -> None:
        self._issues: list[ValidationIssue] = []

    @property
    def issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(self._issues)

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "ERROR" for issue in self._issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity == "WARNING" for issue in self._issues)

    def error(self, code: str, message: str, location: str = "") -> None:
        self._issues.append(ValidationIssue("ERROR", code, message, location))

    def warning(self, code: str, message: str, location: str = "") -> None:
        self._issues.append(ValidationIssue("WARNING", code, message, location))

    def extend(self, issues: Iterable[ValidationIssue]) -> None:
        self._issues.extend(issues)

    def capture(
        self,
        code: str,
        operation: Callable[[], object],
        *,
        message: str,
    ) -> object | None:
        """捕捉済みエラーを問題へ変換し、他の検査を継続する。"""
        try:
            return operation()
        except StoryPipelineError as error:
            self.error(code, f"{message}: {error.reason}", error.location)
            return None
