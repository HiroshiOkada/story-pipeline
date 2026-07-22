from __future__ import annotations

from decimal import Decimal
import unittest

from story_pipeline.production_validation import (
    StoryMeasurement,
    ValidationLimits,
    measure_run,
    may_start_next_story,
    story_failures,
)


def run_record(*, cost: float | None = 0.1, attempts: int = 1) -> dict[str, object]:
    usage = None if cost is None else {
        "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
        "cached_tokens": None, "reasoning_tokens": None, "cost_usd": cost,
    }
    attempt = {
        "model_reference": "default", "api_model": "model", "attempt": 1,
        "maximum_attempts": attempts, "started_at": "2026-07-22T00:00:00Z",
        "finished_at": "2026-07-22T00:00:01Z", "elapsed_ms": 1000,
        "result": "completed", "failure_kind": None, "wait_ms": 0,
    }
    call = {
        "role": "writer", "category": "generation", "usage": usage,
        "elapsed_ms": 1000, "transport_attempts": [dict(attempt) for _ in range(attempts)],
    }
    return {"request_number": 4, "model_calls": [call]}


class ProductionValidationPolicyTest(unittest.TestCase):
    def test_measurement_keeps_missing_cost_distinct_and_counts_draft_writer(self) -> None:
        run = run_record(cost=None)
        run["unsafe_fixture"] = "top-secret-in-response"
        measured = measure_run(run, "drafting")

        self.assertIsNone(measured.cost_usd)
        self.assertEqual(measured.draft_writer_calls, 1)
        story = StoryMeasurement((measured,), 1.0)
        self.assertEqual(story_failures(story), ("USAGE_COST_MISSING",))
        self.assertFalse(may_start_next_story(story.cost_usd))
        self.assertNotIn("top-secret-in-response", repr(measured))

    def test_zero_cost_allows_next_story(self) -> None:
        story = StoryMeasurement((measure_run(run_record(cost=0), "concept"),), 10.0)

        self.assertEqual(story.cost_usd, Decimal("0.0"))
        self.assertEqual(story_failures(story), ())
        self.assertTrue(may_start_next_story(story.cost_usd))

    def test_every_limit_has_a_stable_failure_code(self) -> None:
        measured = measure_run(run_record(cost=2.1, attempts=2), "drafting")
        story = StoryMeasurement((measured, measured, measured), 1201.0)
        limits = ValidationLimits(logical_calls=2, draft_writer_calls=2)

        self.assertEqual(
            story_failures(story, cumulative_cost_before=Decimal("1"), limits=limits),
            (
                "STORY_COST_EXCEEDED", "TOTAL_COST_EXCEEDED", "LOGICAL_CALLS_EXCEEDED",
                "DRAFT_WRITER_CALLS_EXCEEDED", "TRANSPORT_RATIO_EXCEEDED",
                "STORY_TIME_EXCEEDED",
            ),
        )

    def test_total_stop_threshold_is_strict(self) -> None:
        self.assertTrue(may_start_next_story(Decimal("3.999")))
        self.assertFalse(may_start_next_story(Decimal("4.00")))


if __name__ == "__main__":
    unittest.main()
