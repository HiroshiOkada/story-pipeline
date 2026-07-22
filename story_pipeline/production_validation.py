"""本番統合試験の実測値と停止条件。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    total_cost_usd: Decimal = Decimal("5.00")
    total_stop_threshold_usd: Decimal = Decimal("4.00")
    story_cost_usd: Decimal = Decimal("2.00")
    logical_calls: int = 30
    draft_writer_calls: int = 2
    transport_ratio: Decimal = Decimal("1.2")
    story_seconds: int = 20 * 60


@dataclass(frozen=True, slots=True)
class RunMeasurement:
    request_number: int
    phase: str
    logical_calls: int
    transport_attempts: int
    draft_writer_calls: int
    cost_usd: Decimal | None
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class StoryMeasurement:
    runs: tuple[RunMeasurement, ...]
    wall_seconds: float

    @property
    def logical_calls(self) -> int:
        return sum(item.logical_calls for item in self.runs)

    @property
    def transport_attempts(self) -> int:
        return sum(item.transport_attempts for item in self.runs)

    @property
    def draft_writer_calls(self) -> int:
        return sum(item.draft_writer_calls for item in self.runs)

    @property
    def cost_usd(self) -> Decimal | None:
        if any(item.cost_usd is None for item in self.runs):
            return None
        return sum((item.cost_usd for item in self.runs if item.cost_usd is not None), Decimal())


def measure_run(run: dict[str, Any], phase: str) -> RunMeasurement:
    """run v3 の詳細値から試験用集計を再構成する。"""
    calls = run["model_calls"]
    costs = [item["usage"]["cost_usd"] if item["usage"] is not None else None for item in calls]
    cost = None if any(value is None for value in costs) else sum(
        (Decimal(str(value)) for value in costs if value is not None), Decimal()
    )
    return RunMeasurement(
        request_number=run["request_number"],
        phase=phase,
        logical_calls=len(calls),
        transport_attempts=sum(len(item["transport_attempts"]) for item in calls),
        draft_writer_calls=sum(
            phase == "drafting" and item["role"] == "writer" and item["category"] == "generation"
            for item in calls
        ),
        cost_usd=cost,
        elapsed_ms=sum(item["elapsed_ms"] for item in calls),
    )


def story_failures(
    story: StoryMeasurement,
    *,
    cumulative_cost_before: Decimal = Decimal(),
    limits: ValidationLimits = ValidationLimits(),
) -> tuple[str, ...]:
    """追加 API 呼び出し前に判定すべき不合格理由を返す。"""
    failures: list[str] = []
    if story.cost_usd is None:
        failures.append("USAGE_COST_MISSING")
    else:
        if story.cost_usd > limits.story_cost_usd:
            failures.append("STORY_COST_EXCEEDED")
        if cumulative_cost_before + story.cost_usd > limits.total_cost_usd:
            failures.append("TOTAL_COST_EXCEEDED")
    if story.logical_calls > limits.logical_calls:
        failures.append("LOGICAL_CALLS_EXCEEDED")
    if story.draft_writer_calls > limits.draft_writer_calls:
        failures.append("DRAFT_WRITER_CALLS_EXCEEDED")
    allowed_transport = Decimal(story.logical_calls) * limits.transport_ratio
    if Decimal(story.transport_attempts) > allowed_transport:
        failures.append("TRANSPORT_RATIO_EXCEEDED")
    if story.wall_seconds > limits.story_seconds:
        failures.append("STORY_TIME_EXCEEDED")
    return tuple(failures)


def may_start_next_story(
    cumulative_cost: Decimal | None,
    *,
    limits: ValidationLimits = ValidationLimits(),
) -> bool:
    """cost 欠落または合計停止閾値到達後の開始を禁止する。"""
    return cumulative_cost is not None and cumulative_cost < limits.total_stop_threshold_usd
