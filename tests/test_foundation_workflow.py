from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.foundation import FOUNDATION_FILES, FOUNDATION_HEADINGS
from story_pipeline.foundation_workflow import produce_foundation
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def foundation_payload(label: str) -> str:
    return json.dumps(
        {
            path: "\n\n".join(f"{heading}\n{label}" for heading in headings) + "\n"
            for path, headings in FOUNDATION_HEADINGS.items()
        },
        ensure_ascii=False,
    )


def evaluation(decision: str, consistency: int) -> str:
    return json.dumps(
        {
            "decision": decision,
            "summary": "採用可能" if decision == "accept" else "改善が必要",
            "issues": [],
            "scores": {
                "request_fit": 5,
                "concept_fit": 5,
                "consistency": consistency,
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


class FoundationWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n基礎設定を作ってください。\n", encoding="utf-8"
        )
        (self.root / "concept.md").write_text("# 構想\n\n海辺の青春短編。\n", encoding="utf-8")
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "continue",
                    "summary": "基礎設定を作る",
                    "targets": [],
                    "required_conditions": ["現代日本の海辺"],
                    "prohibited_changes": ["超能力を出さない"],
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

    def test_mock_workflow_regenerates_revises_reviews_and_adopts_bundle(self) -> None:
        client = FakeClient(
            [
                json.dumps({"world.md": "不完全"}),
                foundation_payload("初稿"),
                evaluation("revise", 3),
                foundation_payload("改稿"),
                evaluation("accept", 5),
            ]
        )
        result = produce_foundation(self.root, self.request, self.interpretation, client)
        self.assertEqual(result.status, "completed")
        self.assertEqual(dict(result.call_counts), {"generation": 2, "review": 2, "revision": 1})
        self.assertEqual(client.roles, ["writer", "writer", "reviewer", "reviser", "reviewer"])
        self.assertIsNotNone(result.best)
        self.assertEqual(tuple(name for name, _ in result.best.candidate.documents), FOUNDATION_FILES)
        self.assertIn("改稿", result.best.candidate.content("canon.md"))
        self.assertFalse(any((self.root / path).exists() for path in FOUNDATION_FILES))

    def test_mock_workflow_returns_awaiting_human_without_revision(self) -> None:
        client = FakeClient([foundation_payload("判断候補"), evaluation("awaiting_human", 3)])
        result = produce_foundation(self.root, self.request, self.interpretation, client)
        self.assertEqual(result.status, "awaiting_human")
        self.assertIsNone(result.best)
        self.assertEqual(dict(result.call_counts)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
