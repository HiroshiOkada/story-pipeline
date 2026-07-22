from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.concept import CONCEPT_HEADINGS
from story_pipeline.concept_workflow import produce_concept
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def concept_body(label: str) -> str:
    return "\n\n".join(f"{heading}\n{label}" for heading in CONCEPT_HEADINGS) + "\n"


def evaluation(decision: str, request_fit: int, consistency: int) -> str:
    return json.dumps(
        {
            "decision": decision,
            "summary": "採用可能" if decision == "accept" else "改善が必要",
            "issues": [],
            "scores": {"request_fit": request_fit, "consistency": consistency},
        },
        ensure_ascii=False,
    )


class FakeClient:
    config = {
        "limits": {"generation_calls": 3, "review_calls": 3, "revision_calls": 3}
    }

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.roles: list[str] = []

    def complete_role(self, role: str, messages: list[dict[str, str]], **_: object) -> CompletionResult:
        self.roles.append(role)
        content = next(self.responses)
        return CompletionResult(ChatResponse(content, "mock", "stop"), f"mock-{role}", 1, ())


class ConceptWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        request_path = self.root / "requests" / "0000.md"
        request_path.write_text("# 作品作成要求\n\n海辺の短編を書いてください。\n", encoding="utf-8")
        state = load_state(self.root)
        self.request = select_request(self.root, state)
        self.interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "create",
                    "summary": "海辺の短編構想",
                    "targets": [],
                    "required_conditions": ["海辺"],
                    "prohibited_changes": [],
                    "additional_material": [],
                    "decision_answers": [],
                    "ambiguities": [],
                    "requested_units": 1,
                    "requested_until": None,
                },
                ensure_ascii=False,
            ),
            self.request.content,
        )

    def test_mock_workflow_regenerates_revises_reviews_and_adopts(self) -> None:
        client = FakeClient(
            [
                "## タイトル\n不完全\n",
                concept_body("初稿"),
                evaluation("revise", 3, 4),
                concept_body("改稿"),
                evaluation("accept", 5, 5),
            ]
        )
        result = produce_concept(self.root, self.request, self.interpretation, client)
        self.assertEqual(result.status, "completed")
        self.assertEqual(dict(result.call_counts), {"generation": 2, "review": 2, "revision": 1})
        self.assertEqual(client.roles, ["writer", "writer", "reviewer", "reviser", "reviewer"])
        self.assertIsNotNone(result.best)
        self.assertIn("改稿", result.best.candidate.content)
        self.assertFalse((self.root / "concept.md").exists())

    def test_mock_workflow_returns_awaiting_human_without_revision(self) -> None:
        client = FakeClient(
            [
                concept_body("判断候補"),
                evaluation("awaiting_human", 3, 3),
            ]
        )
        result = produce_concept(self.root, self.request, self.interpretation, client)
        self.assertEqual(result.status, "awaiting_human")
        self.assertIsNone(result.best)
        self.assertEqual(dict(result.call_counts)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
