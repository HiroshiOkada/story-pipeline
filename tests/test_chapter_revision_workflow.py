from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.chapter_revision import CHAPTER_SCORE_NAMES, DEFAULT_CHAPTER_REVISION_CONTEXT
from story_pipeline.chapter_revision_workflow import produce_chapter_revision
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def evaluation(decision: str, complete: bool) -> str:
    return json.dumps({
        "decision": decision, "complete": complete,
        "reason": "完成" if complete else "反復表現を直す必要がある",
        "summary": "採用可能" if complete else "局所改稿が必要", "issues": [],
        "scores": {name: (5 if complete else 3) for name in CHAPTER_SCORE_NAMES},
        "human_decision": None,
    }, ensure_ascii=False)


class FakeClient:
    config = {"limits": {"review_calls": 4, "revision_calls": 3}}

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.roles: list[str] = []

    def complete_role(self, role: str, messages: list[dict[str, str]], **_: object) -> CompletionResult:
        self.roles.append(role)
        return CompletionResult(ChatResponse(next(self.responses), "mock", "stop"), f"mock-{role}", 1, ())


class ChapterRevisionWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text("# 要求\n\n第1章を完成させる。\n", encoding="utf-8")
        for path in DEFAULT_CHAPTER_REVISION_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        (self.root / "chapters" / "0001.md").write_text(
            "# 第1章\n\n## 収録話\n0001-0002\n\n## 完成後のあらすじ\n未作成\n", encoding="utf-8"
        )
        for number in (1, 2):
            (self.root / "episodes" / f"{number:04d}.md").write_text(
                f"## 話タイトル\n{number}\n\n## 本文\n出来事{number}が起きた。\n", encoding="utf-8"
            )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(json.dumps({
            "kind": "continue", "summary": "第1章を完成", "targets": [],
            "required_conditions": [], "prohibited_changes": [], "additional_material": [],
            "decision_answers": [], "ambiguities": [], "requested_units": 1, "requested_until": None,
        }, ensure_ascii=False), self.request.content)

    def test_mock_workflow_revises_rechecks_and_builds_completion_update(self) -> None:
        client = FakeClient([
            evaluation("revise", False),
            json.dumps({"revisions": [{
                "path": "episodes/0001.md", "original": "出来事1が起きた。",
                "replacement": "出来事1を経て二人は歩み寄った。", "rationale": "人物変化を明確化",
            }]}, ensure_ascii=False),
            evaluation("accept", True),
            json.dumps({"summary": "二つの出来事を経て二人は歩み寄った。", "evidence": ["出来事1を経て二人は歩み寄った。", "出来事2が起きた。"]}, ensure_ascii=False),
        ])
        result = produce_chapter_revision(
            self.root, self.request, self.interpretation, 1, client,
            all_chapters_complete=True,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(dict(result.call_counts), {"review": 2, "revision": 1, "summary": 1})
        self.assertEqual(client.roles, ["reviewer", "reviser", "reviewer", "reviewer"])
        self.assertEqual(result.completion_update.next_phase, "final_revision")
        self.assertIn("歩み寄った", result.best.documents[0][1])
        self.assertNotIn("歩み寄った", (self.root / "episodes" / "0001.md").read_text(encoding="utf-8"))

    def test_mock_workflow_returns_awaiting_human_without_revision(self) -> None:
        payload = json.loads(evaluation("revise", False))
        payload["decision"] = "awaiting_human"
        payload["summary"] = "三話の再構成が必要"
        payload["human_decision"] = {"question": "再構成しますか", "reason": "複数話へ波及", "choices": ["維持", "再構成"]}
        result = produce_chapter_revision(
            self.root, self.request, self.interpretation, 1,
            FakeClient([json.dumps(payload, ensure_ascii=False)]),
        )
        self.assertEqual(result.status, "awaiting_human")
        self.assertIsNone(result.best)
        self.assertIsNone(result.completion_update)


if __name__ == "__main__":
    unittest.main()
