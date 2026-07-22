from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.errors import StoryPipelineError
from story_pipeline.final_revision import (
    DEFAULT_FINAL_REVISION_CONTEXT,
    EvaluatedFinalRevision,
    FINAL_SCORE_NAMES,
    build_final_revision_context,
    build_final_revision_messages,
    build_final_completion_update,
    check_final_revision_candidate,
    final_evaluation_response_format,
    parse_final_evaluation,
    parse_final_revision_candidate,
    run_final_revision_loop,
    select_best_final_revision,
)
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


class FinalRevisionContextTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n作品全体を完成させてください。\n", encoding="utf-8"
        )
        for path in DEFAULT_FINAL_REVISION_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        for number in (1, 2):
            (self.root / "chapters" / f"{number:04d}.md").write_text(
                f"# 第{number}章\n\n## 接続条件\n第{number}章の接続。\n\n"
                f"## 完成後のあらすじ\n第{number}章の出来事。\n", encoding="utf-8"
            )
        for number in (1, 2, 3, 4):
            (self.root / "episodes" / f"{number:04d}.md").write_text(
                f"## 話タイトル\n第{number}話\n\n## 本文\n出来事{number}。\n", encoding="utf-8"
            )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(json.dumps({
            "kind": "continue", "summary": "作品全体を完成させる", "targets": [],
            "required_conditions": ["友情"], "prohibited_changes": ["夢落ち"],
            "additional_material": [], "decision_answers": [], "ambiguities": [],
            "requested_units": 1, "requested_until": None,
        }, ensure_ascii=False), self.request.content)

    def test_uses_full_text_when_all_episodes_fit(self) -> None:
        context = build_final_revision_context(
            self.root, self.request, self.interpretation, max_full_text_characters=10_000
        )
        self.assertEqual(context.mode, "full_text")
        self.assertEqual(len(context.chapter_paths), 2)
        self.assertEqual(len(context.episode_paths), 4)
        for path in (*DEFAULT_FINAL_REVISION_CONTEXT, *context.chapter_paths, *context.episode_paths):
            digest = hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            self.assertIn(f"path={path} sha256={digest}", context.messages[4]["content"])

    def test_falls_back_to_chapter_summaries_when_full_text_is_too_large(self) -> None:
        context = build_final_revision_context(
            self.root, self.request, self.interpretation, max_full_text_characters=1
        )
        self.assertEqual(context.mode, "chapter_summaries")
        self.assertIn("chapters/0001.md", context.messages[4]["content"])
        self.assertNotIn("episodes/0001.md", context.messages[4]["content"])

    def test_summary_mode_rejects_incomplete_chapter_summary(self) -> None:
        (self.root / "chapters" / "0002.md").write_text(
            "# 第2章\n\n## 接続条件\n接続。\n\n## 完成後のあらすじ\n未作成\n",
            encoding="utf-8",
        )
        with self.assertRaises(StoryPipelineError):
            build_final_revision_context(
                self.root, self.request, self.interpretation, max_full_text_characters=1
            )

    def test_evaluation_requires_explicit_completion_and_all_scores(self) -> None:
        payload = {
            "decision": "accept", "complete": True, "reason": "結末まで成立した",
            "summary": "完成", "issues": [],
            "scores": {name: 5 for name in FINAL_SCORE_NAMES}, "human_decision": None,
        }
        self.assertTrue(parse_final_evaluation(json.dumps(payload, ensure_ascii=False)).adoptable)
        payload["complete"] = False
        self.assertFalse(parse_final_evaluation(json.dumps(payload, ensure_ascii=False)).adoptable)
        del payload["scores"]["ending"]
        with self.assertRaises(StoryPipelineError):
            parse_final_evaluation(json.dumps(payload, ensure_ascii=False))

    def test_awaiting_human_requires_structured_large_change_decision(self) -> None:
        payload = {
            "decision": "awaiting_human", "complete": False, "reason": "結末変更が必要",
            "summary": "根本方針に影響", "issues": [],
            "scores": {name: 3 for name in FINAL_SCORE_NAMES},
            "human_decision": {"question": "結末を変更しますか", "reason": "複数章へ波及", "choices": ["維持", "変更"]},
        }
        evaluation = parse_final_evaluation(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(evaluation.human_decision.choices, ("維持", "変更"))
        payload["human_decision"] = None
        with self.assertRaises(StoryPipelineError):
            parse_final_evaluation(json.dumps(payload, ensure_ascii=False))

    def test_evaluation_schema_is_strict(self) -> None:
        schema = final_evaluation_response_format()["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]["scores"]["required"]), set(FINAL_SCORE_NAMES))

    def test_local_revision_changes_only_unique_quote(self) -> None:
        context = build_final_revision_context(
            self.root, self.request, self.interpretation, max_full_text_characters=10_000
        )
        candidate = parse_final_revision_candidate(json.dumps({"revisions": [{
            "path": "episodes/0002.md", "original": "出来事2。", "replacement": "出来事2を経て和解した。",
            "rationale": "人物変化を明確化",
        }]}, ensure_ascii=False), generation=1, model_reference="mock", input_hashes=context.input_hashes)
        originals = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        checked = check_final_revision_candidate(candidate, context, originals)
        self.assertTrue(checked.accepted)
        self.assertIn("和解した", dict(checked.documents)["episodes/0002.md"])
        self.assertEqual(dict(checked.documents)["episodes/0001.md"], dict(originals)["episodes/0001.md"])

    def test_summary_mode_refuses_automatic_episode_revision(self) -> None:
        context = build_final_revision_context(
            self.root, self.request, self.interpretation, max_full_text_characters=1
        )
        candidate = parse_final_revision_candidate(json.dumps({"revisions": [{
            "path": "episodes/0001.md", "original": "出来事1。", "replacement": "変更。", "rationale": "修正",
        }]}, ensure_ascii=False), generation=1, model_reference="mock", input_hashes=())
        originals = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        checked = check_final_revision_candidate(candidate, context, originals)
        self.assertFalse(checked.accepted)
        self.assertEqual(checked.issues[0].code, "FULL_TEXT_REQUIRED")

    def test_revision_loop_rechecks_and_selects_completed_novel(self) -> None:
        context = build_final_revision_context(
            self.root, self.request, self.interpretation, max_full_text_characters=10_000
        )
        scores = {name: 4 for name in FINAL_SCORE_NAMES}
        revise_evaluation = parse_final_evaluation(json.dumps({
            "decision": "revise", "complete": False, "reason": "人物変化が弱い",
            "summary": "改稿が必要", "issues": [], "scores": scores, "human_decision": None,
        }, ensure_ascii=False))
        accepted = parse_final_evaluation(json.dumps({
            "decision": "accept", "complete": True, "reason": "作品として完成",
            "summary": "採用", "issues": [], "scores": scores, "human_decision": None,
        }, ensure_ascii=False))
        originals = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        initial = EvaluatedFinalRevision(None, originals, revise_evaluation)

        def revise(current, count):
            messages = build_final_revision_messages(context, current)
            self.assertIn("BEGIN NOVEL EPISODES", messages[-3]["content"])
            candidate = parse_final_revision_candidate(json.dumps({"revisions": [{
                "path": "episodes/0004.md", "original": "出来事4。", "replacement": "出来事4を経て成長した。",
                "rationale": "人物変化を明確化",
            }]}, ensure_ascii=False), generation=count, model_reference="mock", input_hashes=(), revision_count=count)
            checked = check_final_revision_candidate(candidate, context, current.documents)
            self.assertTrue(checked.accepted)
            return candidate, checked.documents

        records = run_final_revision_loop(initial, 2, revise, lambda _: accepted)
        self.assertEqual(len(records), 2)
        self.assertIs(select_best_final_revision(records), records[-1])

    def test_best_final_candidate_prefers_fewer_revisions_after_scores(self) -> None:
        evaluation = parse_final_evaluation(json.dumps({
            "decision": "accept", "complete": True, "reason": "完成", "summary": "採用",
            "issues": [], "scores": {name: 5 for name in FINAL_SCORE_NAMES}, "human_decision": None,
        }, ensure_ascii=False))
        candidate = parse_final_revision_candidate(json.dumps({"revisions": [{
            "path": "episodes/0001.md", "original": "A", "replacement": "B", "rationale": "修正",
        }]}), generation=2, model_reference="mock", input_hashes=(), revision_count=1)
        original = EvaluatedFinalRevision(None, (), evaluation)
        revised = EvaluatedFinalRevision(candidate, (), evaluation)
        self.assertIs(select_best_final_revision((revised, original)), original)

    def test_completion_update_requires_all_artifacts_and_no_pending_items(self) -> None:
        context = build_final_revision_context(
            self.root, self.request, self.interpretation, max_full_text_characters=10_000
        )
        evaluation = parse_final_evaluation(json.dumps({
            "decision": "accept", "complete": True, "reason": "結末まで成立", "summary": "完成",
            "issues": [{
                "severity": "note", "category": "style", "location": "episodes/0002.md",
                "evidence": "簡潔", "instruction": "好みの範囲",
            }], "scores": {name: 5 for name in FINAL_SCORE_NAMES}, "human_decision": None,
        }, ensure_ascii=False))
        documents = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        accepted = EvaluatedFinalRevision(None, documents, evaluation)
        update = build_final_completion_update(
            context, accepted, completed_chapters=(1, 2), completed_episodes=(1, 2, 3, 4)
        )
        self.assertEqual(update.phase, "completed")
        self.assertIsNone(update.current_chapter)
        self.assertEqual(len(update.remaining_notes), 1)
        with self.assertRaises(ValueError):
            build_final_completion_update(
                context, accepted, completed_chapters=(1,), completed_episodes=(1, 2, 3, 4)
            )
        with self.assertRaises(ValueError):
            build_final_completion_update(
                context, accepted, completed_chapters=(1, 2), completed_episodes=(1, 2, 3, 4),
                pending_decisions=({"id": "decision"},),
            )


if __name__ == "__main__":
    unittest.main()
