from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.drafting import DEFAULT_DRAFTING_CONTEXT
from story_pipeline.drafting_workflow import produce_draft
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def draft_payload(label: str) -> str:
    return json.dumps({
        "path": "episodes/0001.md",
        "content": f"## 話タイトル\n潮風\n\n## 本文\n{label}" + "海" * 92 + "。\n",
    }, ensure_ascii=False)


def evaluation(decision: str, consistency: int) -> str:
    return json.dumps({
        "decision": decision, "summary": "採用可能" if decision == "accept" else "改善が必要",
        "issues": [], "scores": {
            "request_fit": 5, "consistency": consistency, "plan_fit": 5,
            "episode_completion": 5, "style_fit": 4, "readability": 4,
        },
    }, ensure_ascii=False)


def knowledge(evidence: str) -> str:
    return json.dumps({
        "canon_facts": [{
            "fact": "共同作業が始まった", "evidence": evidence,
            "source": "episodes/0001.md", "established_at": "第0001話", "people": ["凪", "湊"],
        }],
        "character_states": [],
    }, ensure_ascii=False)


class FakeClient:
    config = {"limits": {"generation_calls": 3, "review_calls": 3, "revision_calls": 3}}

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.roles: list[str] = []

    def complete_role(self, role: str, messages: list[dict[str, str]], **_: object) -> CompletionResult:
        self.roles.append(role)
        return CompletionResult(
            ChatResponse(next(self.responses), "mock", "stop"), f"mock-{role}", 1, ()
        )


class DraftingWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n第1話を執筆してください。\n", encoding="utf-8"
        )
        for path in DEFAULT_DRAFTING_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        (self.root / "episode_plans" / "0001.md").write_text(
            "# 第1話計画\n\n## 目標文字数\n100字\n", encoding="utf-8"
        )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(json.dumps({
            "kind": "continue", "summary": "第1話を執筆", "targets": [],
            "required_conditions": ["友情"], "prohibited_changes": ["夢落ち"],
            "additional_material": [], "decision_answers": [], "ambiguities": [],
            "requested_units": 1, "requested_until": None,
        }, ensure_ascii=False), self.request.content)

    def test_mock_workflow_regenerates_revises_and_extracts_verified_knowledge(self) -> None:
        revised_label = "改稿で二人は看板を直し始めた。"
        client = FakeClient([
            json.dumps({"path": "episodes/0001.md", "content": "不完全"}),
            draft_payload("初稿"), evaluation("revise", 3),
            draft_payload(revised_label), evaluation("accept", 5), knowledge(revised_label),
        ])
        result = produce_draft(self.root, self.request, self.interpretation, 1, client)
        self.assertEqual(result.status, "completed")
        self.assertEqual(dict(result.call_counts), {"generation": 2, "review": 3, "revision": 1})
        self.assertEqual(
            client.roles, ["writer", "writer", "reviewer", "reviser", "reviewer", "reviewer"]
        )
        self.assertIsNotNone(result.best)
        self.assertIn("改稿", result.best.candidate.content)
        self.assertEqual(result.knowledge_update.canon_facts[0].source, "episodes/0001.md")
        self.assertFalse((self.root / "episodes" / "0001.md").exists())
        self.assertEqual(result.checkpoint_path, ".story-pipeline/checkpoints/0000/draft.json")
        self.assertTrue((self.root / result.checkpoint_path).is_file())
        self.assertIn("MISSING_HEADING", {item.code for item in result.diagnostics})
        self.assertNotIn("不完全", " ".join(item.reason for item in result.diagnostics))

    def test_mock_workflow_returns_awaiting_human_without_knowledge_call(self) -> None:
        client = FakeClient([draft_payload("判断候補"), evaluation("awaiting_human", 3)])
        result = produce_draft(self.root, self.request, self.interpretation, 1, client)
        self.assertEqual(result.status, "awaiting_human")
        self.assertIsNone(result.best)
        self.assertIsNone(result.knowledge_update)
        self.assertEqual(dict(result.call_counts)["review"], 1)

    def test_knowledge_failure_resume_skips_writer_and_draft_reviewer(self) -> None:
        accepted_label = "二人は看板を直し始めた。"
        first = FakeClient([
            draft_payload(accepted_label), evaluation("accept", 5), "{}", "{}",
        ])

        failed = produce_draft(self.root, self.request, self.interpretation, 1, first)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(first.roles, ["writer", "reviewer", "reviewer", "reviewer"])
        second = FakeClient([knowledge(accepted_label)])

        resumed = produce_draft(self.root, self.request, self.interpretation, 1, second)

        self.assertEqual(resumed.status, "completed")
        self.assertEqual(second.roles, ["reviewer"])
        self.assertEqual([item.purpose for item in resumed.calls], ["knowledge"])
        self.assertIn("CHECKPOINT_REUSED", {item.code for item in resumed.diagnostics})

    def test_changed_checkpoint_input_regenerates_draft(self) -> None:
        accepted_label = "二人は看板を直し始めた。"
        first = FakeClient([
            draft_payload(accepted_label), evaluation("accept", 5), "{}", "{}",
        ])
        self.assertEqual(
            produce_draft(self.root, self.request, self.interpretation, 1, first).status,
            "failed",
        )
        (self.root / "canon.md").write_text("# canon.md\n\n人間が更新した内容\n", encoding="utf-8")
        second = FakeClient([
            draft_payload("新しい本文"), evaluation("accept", 5), knowledge("新しい本文"),
        ])

        resumed = produce_draft(self.root, self.request, self.interpretation, 1, second)

        self.assertEqual(resumed.status, "completed")
        self.assertEqual(second.roles, ["writer", "reviewer", "reviewer"])
        self.assertNotIn("CHECKPOINT_REUSED", {item.code for item in resumed.diagnostics})


if __name__ == "__main__":
    unittest.main()
