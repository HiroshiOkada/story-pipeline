from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.errors import StoryPipelineError
from story_pipeline.final_revision import (
    DEFAULT_FINAL_REVISION_CONTEXT,
    FINAL_SCORE_NAMES,
    build_final_revision_context,
    final_evaluation_response_format,
    parse_final_evaluation,
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


if __name__ == "__main__":
    unittest.main()
