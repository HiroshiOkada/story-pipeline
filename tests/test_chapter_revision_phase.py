from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.chapter_revision import (
    CHAPTER_SCORE_NAMES,
    DEFAULT_CHAPTER_REVISION_CONTEXT,
    build_chapter_revision_context,
    chapter_evaluation_response_format,
    chapter_revision_response_format,
    check_chapter_revision_candidate,
    EvaluatedChapterRevision,
    build_chapter_revision_messages,
    build_chapter_completion_update,
    build_chapter_summary_messages,
    chapter_summary_evidence_options,
    chapter_summary_response_format,
    parse_chapter_evaluation,
    parse_chapter_revision_candidate,
    run_chapter_revision_loop,
    select_best_chapter_revision,
)
from story_pipeline.errors import StoryPipelineError
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


class ChapterRevisionContextTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n第1章を仕上げてください。\n", encoding="utf-8"
        )
        for path in DEFAULT_CHAPTER_REVISION_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        for number in (1, 2):
            (self.root / "chapters" / f"{number:04d}.md").write_text(
                f"# 第{number}章\n\n## 収録話\n0001-0002\n\n## 完成後のあらすじ\n未作成\n",
                encoding="utf-8",
            )
        for number in (1, 2):
            (self.root / "episodes" / f"{number:04d}.md").write_text(
                f"## 話タイトル\n第{number}話\n\n## 本文\n本文{number}\n", encoding="utf-8"
            )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(json.dumps({
            "kind": "continue", "summary": "第1章を完成させる", "targets": [],
            "required_conditions": ["友情"], "prohibited_changes": ["夢落ち"],
            "additional_material": [], "decision_answers": [], "ambiguities": [],
            "requested_units": 1, "requested_until": None,
        }, ensure_ascii=False), self.request.content)

    def test_builds_context_with_all_episodes_and_adjacent_chapter(self) -> None:
        context = build_chapter_revision_context(
            self.root, self.request, self.interpretation, 1
        )
        self.assertEqual(context.chapter_path, "chapters/0001.md")
        self.assertEqual(context.episode_paths, ("episodes/0001.md", "episodes/0002.md"))
        self.assertIsNone(context.previous_chapter_path)
        self.assertEqual(context.next_chapter_path, "chapters/0002.md")
        for path in (*DEFAULT_CHAPTER_REVISION_CONTEXT, "chapters/0001.md", "episodes/0001.md", "episodes/0002.md", "chapters/0002.md"):
            digest = hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            self.assertIn(f"path={path} sha256={digest}", context.messages[3]["content"])

    def test_rejects_chapter_without_episode_range(self) -> None:
        (self.root / "chapters" / "0001.md").write_text("# 第1章\n", encoding="utf-8")
        with self.assertRaises(StoryPipelineError):
            build_chapter_revision_context(self.root, self.request, self.interpretation, 1)

    def test_evaluation_requires_completion_and_all_quality_scores(self) -> None:
        payload = {
            "decision": "accept", "complete": True, "reason": "章として完結した",
            "summary": "採用可能", "issues": [],
            "scores": {name: 5 for name in CHAPTER_SCORE_NAMES}, "human_decision": None,
        }
        self.assertTrue(parse_chapter_evaluation(json.dumps(payload, ensure_ascii=False)).adoptable)
        del payload["scores"]["timeline"]
        with self.assertRaises(StoryPipelineError):
            parse_chapter_evaluation(json.dumps(payload, ensure_ascii=False))

    def test_awaiting_human_requires_structured_decision(self) -> None:
        payload = {
            "decision": "awaiting_human", "complete": False, "reason": "再構成が必要",
            "summary": "章順の変更が必要", "issues": [],
            "scores": {name: 3 for name in CHAPTER_SCORE_NAMES},
            "human_decision": {"question": "章を再構成しますか", "reason": "三話へ波及するため", "choices": ["維持", "再構成"]},
        }
        evaluation = parse_chapter_evaluation(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(evaluation.human_decision.choices, ("維持", "再構成"))
        payload["human_decision"] = None
        with self.assertRaises(StoryPipelineError):
            parse_chapter_evaluation(json.dumps(payload, ensure_ascii=False))

    def test_schema_pins_all_chapter_quality_scores(self) -> None:
        schema = chapter_evaluation_response_format()["json_schema"]["schema"]
        self.assertEqual(set(schema["properties"]["scores"]["required"]), set(CHAPTER_SCORE_NAMES))
        self.assertFalse(schema["additionalProperties"])

    def test_local_revision_changes_only_unique_quote_in_target_episode(self) -> None:
        context = build_chapter_revision_context(self.root, self.request, self.interpretation, 1)
        payload = {"revisions": [{
            "path": "episodes/0001.md", "original": "本文1", "replacement": "改稿本文1",
            "rationale": "人物変化を明確にする",
        }]}
        candidate = parse_chapter_revision_candidate(
            json.dumps(payload, ensure_ascii=False), generation=1,
            model_reference="mock", input_hashes=context.input_hashes,
        )
        originals = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        checked = check_chapter_revision_candidate(candidate, context, originals)
        self.assertTrue(checked.accepted)
        self.assertIn("改稿本文1", dict(checked.documents)["episodes/0001.md"])
        self.assertEqual(dict(checked.documents)["episodes/0002.md"], dict(originals)["episodes/0002.md"])

    def test_local_revision_rejects_out_of_scope_and_non_unique_quote(self) -> None:
        context = build_chapter_revision_context(self.root, self.request, self.interpretation, 1)
        payload = {"revisions": [
            {"path": "episodes/0003.md", "original": "本文", "replacement": "改稿", "rationale": "対象外"},
            {"path": "episodes/0001.md", "original": "話", "replacement": "章", "rationale": "複数一致"},
        ]}
        candidate = parse_chapter_revision_candidate(
            json.dumps(payload, ensure_ascii=False), generation=1, model_reference="mock", input_hashes=(),
        )
        originals = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        codes = {issue.code for issue in check_chapter_revision_candidate(candidate, context, originals).issues}
        self.assertEqual(codes, {"TARGET_OUT_OF_SCOPE", "ORIGINAL_NOT_UNIQUE"})

    def test_revision_schema_rejects_unknown_fields(self) -> None:
        schema = chapter_revision_response_format()["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["revisions"]["minItems"], 1)

    def test_revision_loop_rechecks_and_selects_completed_candidate(self) -> None:
        context = build_chapter_revision_context(self.root, self.request, self.interpretation, 1)
        scores = {name: 4 for name in CHAPTER_SCORE_NAMES}
        revise_evaluation = parse_chapter_evaluation(json.dumps({
            "decision": "revise", "complete": False, "reason": "反復がある",
            "summary": "改稿が必要", "issues": [], "scores": scores, "human_decision": None,
        }, ensure_ascii=False))
        accepted_payload = {
            "decision": "accept", "complete": True, "reason": "章として完結",
            "summary": "採用可能", "issues": [], "scores": scores, "human_decision": None,
        }
        accepted = parse_chapter_evaluation(json.dumps(accepted_payload, ensure_ascii=False))
        originals = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        initial = EvaluatedChapterRevision(None, originals, revise_evaluation)

        def revise(current, count):
            messages = build_chapter_revision_messages(context, current)
            self.assertIn("BEGIN CHAPTER EPISODES", messages[-3]["content"])
            candidate = parse_chapter_revision_candidate(json.dumps({"revisions": [{
                "path": "episodes/0001.md", "original": "本文1", "replacement": "改稿本文1",
                "rationale": "反復を解消",
            }]}, ensure_ascii=False), generation=count, model_reference="mock", input_hashes=(), revision_count=count)
            checked = check_chapter_revision_candidate(candidate, context, current.documents)
            self.assertTrue(checked.accepted)
            return candidate, checked.documents

        records = run_chapter_revision_loop(initial, 2, revise, lambda _: accepted)
        self.assertEqual(len(records), 2)
        self.assertIs(select_best_chapter_revision(records), records[-1])

    def test_best_candidate_prefers_fewer_revisions_after_scores(self) -> None:
        scores = {name: 5 for name in CHAPTER_SCORE_NAMES}
        evaluation = parse_chapter_evaluation(json.dumps({
            "decision": "accept", "complete": True, "reason": "完成", "summary": "採用",
            "issues": [], "scores": scores, "human_decision": None,
        }, ensure_ascii=False))
        candidate = parse_chapter_revision_candidate(json.dumps({"revisions": [{
            "path": "episodes/0001.md", "original": "A", "replacement": "B", "rationale": "修正",
        }]}), generation=2, model_reference="mock", input_hashes=(), revision_count=1)
        original = EvaluatedChapterRevision(None, (), evaluation)
        revised = EvaluatedChapterRevision(candidate, (), evaluation)
        self.assertIs(select_best_chapter_revision((revised, original)), original)

    def test_completion_update_verifies_evidence_and_updates_summary_and_state(self) -> None:
        context = build_chapter_revision_context(self.root, self.request, self.interpretation, 1)
        scores = {name: 5 for name in CHAPTER_SCORE_NAMES}
        evaluation = parse_chapter_evaluation(json.dumps({
            "decision": "accept", "complete": True, "reason": "完成", "summary": "採用",
            "issues": [], "scores": scores, "human_decision": None,
        }, ensure_ascii=False))
        documents = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        accepted = EvaluatedChapterRevision(None, documents, evaluation)
        messages = build_chapter_summary_messages(context, accepted)
        self.assertIn("BEGIN ACCEPTED CHAPTER", messages[1]["content"])
        payload = json.dumps({"summary": "二つの出来事を経て関係が進展した。", "evidence": ["本文1", "本文2"]}, ensure_ascii=False)
        update = build_chapter_completion_update(
            payload, context=context, accepted=accepted,
            chapter_content=(self.root / context.chapter_path).read_text(encoding="utf-8"),
            completed_chapters=(), all_chapters_complete=False,
        )
        self.assertIn("二つの出来事", update.chapter_content)
        self.assertNotIn("未作成", update.chapter_content)
        self.assertEqual(update.completed_chapters, (1,))
        self.assertEqual(update.next_chapter, 2)
        self.assertEqual(update.next_phase, "episode_planning")

    def test_completion_update_rejects_unsupported_evidence(self) -> None:
        context = build_chapter_revision_context(self.root, self.request, self.interpretation, 1)
        scores = {name: 5 for name in CHAPTER_SCORE_NAMES}
        evaluation = parse_chapter_evaluation(json.dumps({
            "decision": "accept", "complete": True, "reason": "完成", "summary": "採用",
            "issues": [], "scores": scores, "human_decision": None,
        }, ensure_ascii=False))
        documents = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        accepted = EvaluatedChapterRevision(None, documents, evaluation)
        with self.assertRaises(StoryPipelineError):
            build_chapter_completion_update(
                json.dumps({"summary": "要約", "evidence": ["本文にない事実"]}, ensure_ascii=False),
                context=context, accepted=accepted,
                chapter_content=(self.root / context.chapter_path).read_text(encoding="utf-8"),
            )

    def test_completion_update_resolves_evidence_with_whitespace_differences(self) -> None:
        context = build_chapter_revision_context(self.root, self.request, self.interpretation, 1)
        scores = {name: 5 for name in CHAPTER_SCORE_NAMES}
        evaluation = parse_chapter_evaluation(json.dumps({
            "decision": "accept", "complete": True, "reason": "完成", "summary": "採用",
            "issues": [], "scores": scores, "human_decision": None,
        }, ensure_ascii=False))
        documents = tuple((path, (self.root / path).read_text(encoding="utf-8")) for path in context.episode_paths)
        accepted = EvaluatedChapterRevision(None, documents, evaluation)

        update = build_chapter_completion_update(
            json.dumps({"summary": "要約", "evidence": ["本 文 1"]}, ensure_ascii=False),
            context=context,
            accepted=accepted,
            chapter_content=(self.root / context.chapter_path).read_text(encoding="utf-8"),
        )

        self.assertEqual(update.evidence, ("本文1",))

    def test_summary_schema_is_strict(self) -> None:
        schema = chapter_summary_response_format(("一意な根拠本文です。",))["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["evidence"]["minItems"], 1)
        self.assertEqual(
            schema["properties"]["evidence"]["items"]["enum"], ["一意な根拠本文です。"]
        )

    def test_summary_evidence_options_exclude_headings_and_duplicates(self) -> None:
        scores = {name: 5 for name in CHAPTER_SCORE_NAMES}
        evaluation = parse_chapter_evaluation(json.dumps({
            "decision": "accept", "complete": True, "reason": "完成", "summary": "採用",
            "issues": [], "scores": scores, "human_decision": None,
        }, ensure_ascii=False))
        accepted = EvaluatedChapterRevision(None, (
            ("episodes/0001.md", "# 第1話\n\n朝に固有の出来事が起きた。\n共通する長い記述です。\n"),
            ("episodes/0002.md", "# 第2話\n\n夜に別の出来事が起きた。\n共通する長い記述です。\n"),
        ), evaluation)

        self.assertEqual(
            chapter_summary_evidence_options(accepted),
            ("朝に固有の出来事が起きた。", "夜に別の出来事が起きた。"),
        )


if __name__ == "__main__":
    unittest.main()
