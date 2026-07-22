from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.final_revision import DEFAULT_FINAL_REVISION_CONTEXT, FINAL_SCORE_NAMES
from story_pipeline.final_revision_workflow import produce_final_revision
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def evaluation(decision: str, complete: bool) -> str:
    return json.dumps({
        "decision": decision, "complete": complete,
        "reason": "完成" if complete else "人物変化が弱い",
        "summary": "完成" if complete else "局所改稿が必要", "issues": [],
        "scores": {name: (5 if complete else 3) for name in FINAL_SCORE_NAMES},
        "human_decision": None,
    }, ensure_ascii=False)


class FakeClient:
    config = {"limits": {"review_calls": 3, "revision_calls": 3}}

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.roles: list[str] = []

    def complete_role(self, role: str, messages: list[dict[str, str]], **_: object) -> CompletionResult:
        self.roles.append(role)
        return CompletionResult(ChatResponse(next(self.responses), "mock", "stop"), f"mock-{role}", 1, ())


class FinalRevisionWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text("# 要求\n\n作品を完成させる。\n", encoding="utf-8")
        for path in DEFAULT_FINAL_REVISION_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        (self.root / "chapters" / "0001.md").write_text(
            "# 第1章\n\n## 接続条件\n一章完結。\n\n## 完成後のあらすじ\n二人が和解した。\n", encoding="utf-8"
        )
        for number in (1, 2):
            (self.root / "episodes" / f"{number:04d}.md").write_text(
                f"## 話タイトル\n{number}\n\n## 本文\n出来事{number}が起きた。\n", encoding="utf-8"
            )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(json.dumps({
            "kind": "continue", "summary": "作品を完成", "targets": [],
            "required_conditions": [], "prohibited_changes": [], "additional_material": [],
            "decision_answers": [], "ambiguities": [], "requested_units": 1, "requested_until": None,
        }, ensure_ascii=False), self.request.content)

    def test_mock_workflow_revises_rechecks_and_returns_completed_update(self) -> None:
        client = FakeClient([
            evaluation("revise", False),
            json.dumps({"revisions": [{
                "path": "episodes/0002.md", "original": "出来事2が起きた。",
                "replacement": "出来事2を経て二人は和解した。", "rationale": "人物変化を明確化",
            }]}, ensure_ascii=False),
            evaluation("accept", True),
        ])
        result = produce_final_revision(
            self.root, self.request, self.interpretation, client,
            completed_chapters=(1,), completed_episodes=(1, 2),
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(dict(result.call_counts), {"review": 2, "revision": 1})
        self.assertEqual(client.roles, ["reviewer", "reviser", "reviewer"])
        self.assertEqual(result.completion_update.phase, "completed")
        self.assertIn("和解した", result.best.documents[1][1])
        self.assertNotIn("和解した", (self.root / "episodes" / "0002.md").read_text(encoding="utf-8"))

    def test_mock_workflow_returns_human_wait_for_root_change(self) -> None:
        payload = json.loads(evaluation("revise", False))
        payload["decision"] = "awaiting_human"
        payload["summary"] = "結末変更が必要"
        payload["human_decision"] = {"question": "結末を変更しますか", "reason": "根本方針へ影響", "choices": ["維持", "変更"]}
        result = produce_final_revision(
            self.root, self.request, self.interpretation,
            FakeClient([json.dumps(payload, ensure_ascii=False)]),
            completed_chapters=(1,), completed_episodes=(1, 2),
        )
        self.assertEqual(result.status, "awaiting_human")
        self.assertIsNone(result.best)
        self.assertIsNone(result.completion_update)


if __name__ == "__main__":
    unittest.main()
