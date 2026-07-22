from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.episode_planning import DEFAULT_EPISODE_PLANNING_CONTEXT, EPISODE_PLAN_HEADINGS
from story_pipeline.episode_planning_workflow import produce_episode_plan
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def plan_payload(label: str) -> str:
    content = "\n\n".join(
        f"{heading}\n{'4000字' if heading == '## 目標文字数' else label}"
        for heading in EPISODE_PLAN_HEADINGS
    ) + "\n"
    return json.dumps(
        {"path": "episode_plans/0001.md", "content": content}, ensure_ascii=False
    )


def evaluation(decision: str, causal: int) -> str:
    return json.dumps(
        {
            "decision": decision,
            "summary": "採用可能" if decision == "accept" else "改善が必要",
            "issues": [],
            "scores": {
                "request_fit": 5, "chapter_fit": 5, "continuity": 5,
                "causal_consistency": causal, "plan_completeness": 4, "length_fit": 5,
            },
        },
        ensure_ascii=False,
    )


class FakeClient:
    config = {"limits": {"generation_calls": 3, "review_calls": 3, "revision_calls": 3}}

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.roles: list[str] = []

    def complete_role(
        self, role: str, messages: list[dict[str, str]], **_: object
    ) -> CompletionResult:
        self.roles.append(role)
        return CompletionResult(
            ChatResponse(next(self.responses), "mock", "stop"), f"mock-{role}", 1, ()
        )


class EpisodePlanningWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n次の話を計画してください。\n", encoding="utf-8"
        )
        for path in DEFAULT_EPISODE_PLANNING_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        (self.root / "chapters" / "0001.md").write_text(
            "# 第一章\n\n## 目的\n再会\n\n## 収録話\n0001〜0003\n", encoding="utf-8"
        )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(
            json.dumps({
                "kind": "continue", "summary": "次話を計画する", "targets": [],
                "required_conditions": ["友情"], "prohibited_changes": ["夢落ち"],
                "additional_material": [], "decision_answers": [], "ambiguities": [],
                "requested_units": 1, "requested_until": None,
            }, ensure_ascii=False),
            self.request.content,
        )

    def test_mock_workflow_regenerates_revises_reviews_and_adopts_plan(self) -> None:
        client = FakeClient([
            json.dumps({"path": "episode_plans/0001.md", "content": "不完全"}),
            plan_payload("初稿"), evaluation("revise", 3),
            plan_payload("改稿"), evaluation("accept", 5),
        ])
        result = produce_episode_plan(
            self.root, self.request, self.interpretation, 1, client
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(dict(result.call_counts), {"generation": 2, "review": 2, "revision": 1})
        self.assertEqual(client.roles, ["planner", "planner", "reviewer", "reviser", "reviewer"])
        self.assertIsNotNone(result.best)
        self.assertIn("改稿", result.best.candidate.content)
        self.assertEqual(result.best.candidate.path, "episode_plans/0001.md")
        self.assertFalse((self.root / "episode_plans" / "0001.md").exists())

    def test_mock_workflow_returns_awaiting_human_without_revision(self) -> None:
        client = FakeClient([plan_payload("判断候補"), evaluation("awaiting_human", 3)])
        result = produce_episode_plan(
            self.root, self.request, self.interpretation, 1, client
        )
        self.assertEqual(result.status, "awaiting_human")
        self.assertIsNone(result.best)
        self.assertEqual(dict(result.call_counts)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
