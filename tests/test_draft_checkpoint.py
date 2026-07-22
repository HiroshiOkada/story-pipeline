from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from story_pipeline.draft_checkpoint import (
    create_pending_checkpoint,
    load_draft_checkpoint,
    reusable_checkpoint,
    write_draft_checkpoint,
)
from story_pipeline.drafting import (
    DraftCandidate,
    DraftEvaluation,
    DraftingContext,
    EvaluatedDraftCandidate,
)
from story_pipeline.errors import StoryPipelineError


class DraftCheckpointTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".story-pipeline").mkdir()
        self.context = DraftingContext(
            1, "episode_plans/0001.md", None, None, 10, 0.2, (),
            (("requests/0000.md", "a" * 64), ("episode_plans/0001.md", "b" * 64)),
        )
        candidate = DraftCandidate(
            "episodes/0001.md", "## 話タイトル\n潮\n\n## 本文\n海辺を歩いた。\n",
            1, 1, "writer", self.context.input_hashes,
        )
        evaluation = DraftEvaluation(
            "accept", "採用可能", (),
            tuple((name, 5) for name in (
                "request_fit", "consistency", "plan_fit", "episode_completion", "style_fit", "readability"
            )),
        )
        self.best = EvaluatedDraftCandidate(candidate, evaluation)

    def test_round_trip_and_exact_input_reuse(self) -> None:
        checkpoint = create_pending_checkpoint(
            0, 1, self.context, self.best, evaluation_model_reference="reviewer",
            now="2026-07-22T01:23:45Z",
        )
        relative = write_draft_checkpoint(self.root, checkpoint)

        loaded = load_draft_checkpoint(self.root, 0)
        reused = reusable_checkpoint(
            loaded,
            request_revision=1,
            target_path="episodes/0001.md",
            input_hashes=dict(self.context.input_hashes),
        )

        self.assertEqual(relative, ".story-pipeline/checkpoints/0000/draft.json")
        self.assertEqual(reused.candidate.content, self.best.candidate.content)

    def test_changed_input_or_revision_is_not_reused(self) -> None:
        checkpoint = create_pending_checkpoint(
            0, 1, self.context, self.best, evaluation_model_reference="reviewer",
            now="2026-07-22T01:23:45Z",
        )
        self.assertIsNone(reusable_checkpoint(
            checkpoint, request_revision=2, target_path="episodes/0001.md",
            input_hashes=dict(self.context.input_hashes),
        ))
        changed = dict(self.context.input_hashes)
        changed["episode_plans/0001.md"] = "c" * 64
        self.assertIsNone(reusable_checkpoint(
            checkpoint, request_revision=1, target_path="episodes/0001.md", input_hashes=changed,
        ))

    def test_candidate_tampering_is_rejected(self) -> None:
        checkpoint = create_pending_checkpoint(
            0, 1, self.context, self.best, evaluation_model_reference="reviewer",
            now="2026-07-22T01:23:45Z",
        )
        write_draft_checkpoint(self.root, checkpoint)
        path = self.root / ".story-pipeline/checkpoints/0000/draft.json"
        text = path.read_text(encoding="utf-8").replace("海辺を歩いた", "山道を歩いた")
        path.write_text(text, encoding="utf-8")

        with self.assertRaises(StoryPipelineError):
            load_draft_checkpoint(self.root, 0)
