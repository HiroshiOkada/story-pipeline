from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.errors import StoryPipelineError
from story_pipeline.state import load_state, validate_state_data


def base_state() -> dict[str, object]:
    """検証を通る最小の state。各テストで不正な箇所だけを書き換える。"""
    return {
        "schema_version": 1,
        "phase": "concept",
        "next_chapter": 1,
        "next_episode": 1,
        "completed_chapters": [],
        "completed_episodes": [],
        "current_chapter": None,
        "pending_reviews": [],
        "pending_decisions": [],
        "last_request": None,
        "active_request": None,
        "updated_at": "2026-01-01T00:00:00Z",
    }


def base_decision() -> dict[str, object]:
    return {
        "id": "dec-1",
        "request": 0,
        "question": "どちらにしますか",
        "reason": "方針が必要",
        "choices": ["案A", "案B"],
        "created_at": "2026-01-01T00:00:00Z",
    }


class StateValidationTest(unittest.TestCase):
    def assert_invalid(self, state: object, location: str) -> StoryPipelineError:
        with self.assertRaises(StoryPipelineError) as raised:
            validate_state_data(state)
        self.assertEqual(raised.exception.location, location)
        self.assertEqual(raised.exception.exit_code, 4)
        return raised.exception

    def test_accepts_base_state(self) -> None:
        state = base_state()
        self.assertIs(validate_state_data(state), state)

    def test_rejects_non_object_state(self) -> None:
        self.assert_invalid([], "/")

    def test_rejects_missing_and_unknown_keys(self) -> None:
        state = base_state()
        del state["phase"]
        self.assert_invalid(state, "/phase")
        state = base_state()
        state["extra"] = True
        self.assert_invalid(state, "/extra")

    def test_rejects_schema_version_other_than_one(self) -> None:
        state = base_state()
        state["schema_version"] = 2
        self.assert_invalid(state, "/schema_version")

    def test_rejects_unknown_phase(self) -> None:
        state = base_state()
        state["phase"] = "resting"
        self.assert_invalid(state, "/phase")

    def test_rejects_number_out_of_range(self) -> None:
        state = base_state()
        state["next_episode"] = 0
        self.assert_invalid(state, "/next_episode")
        state = base_state()
        state["next_chapter"] = 10000
        self.assert_invalid(state, "/next_chapter")

    def test_rejects_boolean_as_number(self) -> None:
        state = base_state()
        state["next_episode"] = True
        self.assert_invalid(state, "/next_episode")

    def test_next_numbers_must_exceed_completed_maximum(self) -> None:
        state = base_state()
        state.update({"completed_chapters": [1, 2], "next_chapter": 2})
        self.assert_invalid(state, "/next_chapter")
        state = base_state()
        state.update({"completed_episodes": [1, 2], "next_episode": 3})
        state.update({"completed_chapters": [1, 2], "next_chapter": 3})
        self.assertIs(validate_state_data(state), state)

    def test_rejects_duplicate_completed_numbers(self) -> None:
        state = base_state()
        state["completed_chapters"] = [1, 1]
        state["next_chapter"] = 2
        self.assert_invalid(state, "/completed_chapters")

    def test_rejects_typed_current_chapter(self) -> None:
        state = base_state()
        state["current_chapter"] = "1"
        self.assert_invalid(state, "/current_chapter")

    def test_request_numbers_allow_zero_but_not_negative(self) -> None:
        state = base_state()
        state["last_request"] = 0
        state["active_request"] = 0
        self.assertIs(validate_state_data(state), state)
        state = base_state()
        state["last_request"] = -1
        self.assert_invalid(state, "/last_request")

    def test_rejects_invalid_timestamp_formats(self) -> None:
        state = base_state()
        state["updated_at"] = "2026-01-01"
        self.assert_invalid(state, "/updated_at")
        state = base_state()
        state["updated_at"] = "2026-13-01T00:00:00Z"
        error = self.assert_invalid(state, "/updated_at")
        self.assertIn("有効な日時", error.reason)

    def test_completed_phase_rejects_pending_items(self) -> None:
        state = base_state()
        state["phase"] = "completed"
        state["pending_reviews"] = [
            {"target_type": "novel", "target_number": None, "reason": "確認"}
        ]
        self.assert_invalid(state, "/pending_reviews")
        state = base_state()
        state["phase"] = "completed"
        state["pending_decisions"] = [base_decision()]
        self.assert_invalid(state, "/pending_decisions")

    def test_review_rules_depend_on_target_type(self) -> None:
        state = base_state()
        state["pending_reviews"] = [
            {"target_type": "novel", "target_number": 1, "reason": "確認"}
        ]
        self.assert_invalid(state, "/pending_reviews/0/target_number")
        state = base_state()
        state["pending_reviews"] = [
            {"target_type": "chapter", "target_number": None, "reason": "確認"}
        ]
        self.assert_invalid(state, "/pending_reviews/0/target_number")
        state = base_state()
        state["pending_reviews"] = [
            {"target_type": "scene", "target_number": 1, "reason": "確認"}
        ]
        self.assert_invalid(state, "/pending_reviews/0/target_type")
        state = base_state()
        state["pending_reviews"] = [
            {"target_type": "episode", "target_number": 1, "reason": "確認"}
        ]
        self.assertIs(validate_state_data(state), state)

    def test_review_requires_exact_keys(self) -> None:
        state = base_state()
        state["pending_reviews"] = [{"target_type": "novel", "target_number": None}]
        self.assert_invalid(state, "/pending_reviews/0/reason")

    def test_decision_ids_must_be_non_empty_and_unique(self) -> None:
        state = base_state()
        state["pending_decisions"] = [base_decision(), base_decision()]
        self.assert_invalid(state, "/pending_decisions/1/id")
        state = base_state()
        decision = base_decision()
        decision["id"] = ""
        state["pending_decisions"] = [decision]
        self.assert_invalid(state, "/pending_decisions/0/id")

    def test_decision_choices_must_be_non_empty_strings(self) -> None:
        state = base_state()
        decision = base_decision()
        decision["choices"] = []
        state["pending_decisions"] = [decision]
        self.assert_invalid(state, "/pending_decisions/0/choices")
        state = base_state()
        decision = base_decision()
        decision["choices"] = ["案A", 2]
        state["pending_decisions"] = [decision]
        self.assert_invalid(state, "/pending_decisions/0/choices/1")

    def test_decision_created_at_must_be_timestamp(self) -> None:
        state = base_state()
        decision = base_decision()
        decision["created_at"] = "yesterday"
        state["pending_decisions"] = [decision]
        self.assert_invalid(state, "/pending_decisions/0/created_at")


class StateFileTest(unittest.TestCase):
    def test_rejects_duplicate_keys_in_state_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / ".story-pipeline"
            directory.mkdir()
            state = base_state()
            source = json.dumps(state, ensure_ascii=False)
            duplicated = source.replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1
            )
            (directory / "state.json").write_text(duplicated, encoding="utf-8")
            with self.assertRaises(StoryPipelineError) as raised:
                load_state(root)
            self.assertIn("重複", raised.exception.reason)

    def test_read_failure_reports_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(StoryPipelineError) as raised:
                load_state(root)
            self.assertEqual(raised.exception.exit_code, 4)
            self.assertIn("state.json", raised.exception.location)


if __name__ == "__main__":
    unittest.main()
