from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from story_pipeline.draft_checkpoint import (
    complete_checkpoint_knowledge,
    create_pending_checkpoint,
    inspect_checkpoint_adoption,
    load_draft_checkpoint,
    mark_checkpoint_adopted,
    prepare_checkpoint_adoption,
    reusable_checkpoint,
    write_draft_checkpoint,
)
from story_pipeline.drafting import (
    CanonFact,
    CharacterStateUpdate,
    DraftCandidate,
    DraftEvaluation,
    DraftingContext,
    DraftKnowledgeUpdate,
    EvaluatedDraftCandidate,
)
from story_pipeline.errors import StoryPipelineError
from story_pipeline.knowledge_adoption import build_draft_adoption_documents, document_hashes


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

    def test_knowledge_documents_detect_partial_then_complete_adoption(self) -> None:
        (self.root / "canon.md").write_text(
            "## 確定事実\n初期事実\n\n## 人物状態\nなし\n", encoding="utf-8"
        )
        (self.root / "characters.md").write_text(
            "## 人物一覧\n凪\n\n## 人物別の目的・変化・口調・状態\n初期状態\n",
            encoding="utf-8",
        )
        checkpoint = create_pending_checkpoint(
            0, 1, self.context, self.best, evaluation_model_reference="reviewer",
            now="2026-07-22T01:23:45Z",
        )
        update = DraftKnowledgeUpdate(
            (CanonFact("海辺を歩いた", "海辺を歩いた。", "episodes/0001.md", "第1話", ("凪",)),),
            (CharacterStateUpdate("凪", "海辺にいる", "海辺を歩いた。", "episodes/0001.md", "第1話"),),
        )
        checkpoint = complete_checkpoint_knowledge(checkpoint, update, now="2026-07-22T01:24:45Z")
        documents = build_draft_adoption_documents(self.root, self.best.candidate, update)
        checkpoint = prepare_checkpoint_adoption(
            checkpoint, document_hashes(documents), now="2026-07-22T01:25:45Z"
        )
        self.assertEqual(inspect_checkpoint_adoption(self.root, checkpoint), "none")

        episode_path, episode_content = documents[0]
        (self.root / episode_path).parent.mkdir(exist_ok=True)
        (self.root / episode_path).write_text(episode_content, encoding="utf-8")
        self.assertEqual(inspect_checkpoint_adoption(self.root, checkpoint), "partial")

        for relative, content in documents[1:]:
            (self.root / relative).write_text(content, encoding="utf-8")
        self.assertEqual(inspect_checkpoint_adoption(self.root, checkpoint), "all")
        adopted = mark_checkpoint_adopted(checkpoint, checkpoint["adoption"]["output_hashes"])
        self.assertEqual(adopted["adoption"]["status"], "adopted")
        self.assertIn("episodes/0001.md の確定事項", (self.root / "canon.md").read_text())
        self.assertIn("凪: 海辺にいる", (self.root / "characters.md").read_text())
