from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse
from story_pipeline.plotting import CHAPTER_HEADINGS, DEFAULT_PLOTTING_CONTEXT, PLOT_HEADINGS
from story_pipeline.plotting_workflow import produce_plotting
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def plotting_payload(label: str) -> str:
    plot = "\n\n".join(
        f"{heading}\n{'chapters/0001.md' if heading == '## 章構成' else label}"
        for heading in PLOT_HEADINGS
    ) + "\n"
    chapter = "\n\n".join(
        f"{heading}\n{'0001〜0003' if heading == '## 収録話' else label}"
        for heading in CHAPTER_HEADINGS
    ) + "\n"
    return json.dumps(
        {
            "plot.md": plot,
            "chapters": [{"path": "chapters/0001.md", "content": chapter}],
        },
        ensure_ascii=False,
    )


def evaluation(decision: str, causal: int) -> str:
    return json.dumps(
        {
            "decision": decision,
            "summary": "採用可能" if decision == "accept" else "改善が必要",
            "issues": [],
            "scores": {
                "request_fit": 5,
                "foundation_fit": 5,
                "causal_consistency": causal,
                "foreshadowing": 4,
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


class PlottingWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n全体構成を作ってください。\n", encoding="utf-8"
        )
        for path in DEFAULT_PLOTTING_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "continue",
                    "summary": "全体構成を作る",
                    "targets": [],
                    "required_conditions": ["友情の再生"],
                    "prohibited_changes": ["超常現象"],
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
                json.dumps({"plot.md": "不完全", "chapters": []}),
                plotting_payload("初稿"),
                evaluation("revise", 3),
                plotting_payload("改稿"),
                evaluation("accept", 5),
            ]
        )
        result = produce_plotting(self.root, self.request, self.interpretation, client)
        self.assertEqual(result.status, "completed")
        self.assertEqual(dict(result.call_counts), {"generation": 2, "review": 2, "revision": 1})
        self.assertEqual(client.roles, ["writer", "writer", "reviewer", "reviser", "reviewer"])
        self.assertIsNotNone(result.best)
        self.assertIn("改稿", result.best.candidate.plot)
        self.assertEqual(result.best.candidate.chapters[0][0], "chapters/0001.md")
        self.assertFalse((self.root / "plot.md").exists())

    def test_mock_workflow_returns_awaiting_human_without_revision(self) -> None:
        client = FakeClient([plotting_payload("判断候補"), evaluation("awaiting_human", 3)])
        result = produce_plotting(self.root, self.request, self.interpretation, client)
        self.assertEqual(result.status, "awaiting_human")
        self.assertIsNone(result.best)
        self.assertEqual(dict(result.call_counts)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
