from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from story_pipeline.workflow_executor import execute_planned_workflow


def planned(phase: str, target: str) -> SimpleNamespace:
    return SimpleNamespace(
        scope=SimpleNamespace(phase=phase, targets=(target,), action="continue"),
        request=SimpleNamespace(number=0),
        interpretation=SimpleNamespace(),
    )


def best(**values: object) -> SimpleNamespace:
    defaults = {
        "documents": (),
        "evaluation": SimpleNamespace(summary="採用可能"),
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


class WorkflowStateTransitionIntegrationTest(unittest.TestCase):
    def test_one_chapter_one_episode_reaches_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "chapters").mkdir()
            (root / "chapters/0001.md").write_text("## 収録話\n0001\n")
            state = {
                "phase": "drafting", "current_chapter": 1,
                "next_chapter": 1, "next_episode": 1,
                "completed_chapters": [], "completed_episodes": [],
                "pending_reviews": [], "pending_decisions": [],
            }
            draft_result = SimpleNamespace(
                status="completed",
                best=best(candidate=SimpleNamespace(path="episodes/0001.md", content="本文")),
                calls=(), candidates=(), reason=None,
            )
            with patch("story_pipeline.workflow_executor.produce_draft", return_value=draft_result):
                drafted = execute_planned_workflow(
                    root, state, planned("drafting", "episodes/0001.md"), SimpleNamespace()
                )
            state.update(drafted.state_updates)
            self.assertEqual(state["phase"], "chapter_revision")

            chapter_result = SimpleNamespace(
                status="completed", best=best(), calls=(), candidates=(), reason=None,
                completion_update=SimpleNamespace(
                    chapter_path="chapters/0001.md", chapter_content="## 収録話\n0001\n"
                ),
            )
            with patch(
                "story_pipeline.workflow_executor.produce_chapter_revision",
                return_value=chapter_result,
            ) as producer:
                revised = execute_planned_workflow(
                    root, state, planned("chapter_revision", "chapters/0001.md"), SimpleNamespace()
                )
            self.assertTrue(producer.call_args.kwargs["all_chapters_complete"])
            state.update(revised.state_updates)
            self.assertEqual(state["phase"], "final_revision")
            self.assertIsNone(state["current_chapter"])

            final_result = SimpleNamespace(
                status="completed", best=best(), calls=(), candidates=(), reason=None,
                completion_update=SimpleNamespace(
                    phase="completed", completed_chapters=(1,), completed_episodes=(1,),
                    current_chapter=None, pending_reviews=(), pending_decisions=(),
                ),
            )
            with patch(
                "story_pipeline.workflow_executor.produce_final_revision",
                return_value=final_result,
            ):
                completed = execute_planned_workflow(
                    root, state, planned("final_revision", "novel"), SimpleNamespace()
                )
            state.update(completed.state_updates)
            self.assertEqual(state["phase"], "completed")

