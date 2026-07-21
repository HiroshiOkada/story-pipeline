from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.context_builder import build_interpretation_messages, load_context_documents
from story_pipeline.decision_resolution import resolve_pending_decisions
from story_pipeline.errors import StoryPipelineError
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_planner import plan_selected_request
from story_pipeline.request_selection import SelectedRequest, select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state
from story_pipeline.work_scope import determine_work_scope


def interpretation_value(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": "continue",
        "summary": "次の自然な単位へ進める",
        "targets": [],
        "required_conditions": [],
        "prohibited_changes": [],
        "additional_material": [],
        "decision_answers": [],
        "ambiguities": [],
        "requested_units": 1,
        "requested_until": None,
    }
    value.update(updates)
    return value


class RequestScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        self.request = self.root / "requests" / "0000.md"
        self.request.write_text("# 作品作成要求\n\n短編を書いてください。\n", encoding="utf-8")
        self.state = load_state(self.root)

    def test_selects_youngest_unprocessed_request_and_resume_first(self) -> None:
        (self.root / ".story-pipeline" / "runs").mkdir()
        (self.root / ".story-pipeline" / "runs" / "0000.json").write_text("{}\n")
        (self.root / "requests" / "0001.md").write_text("続きを書いてください。\n")
        selected = select_request(self.root, self.state)
        self.assertEqual((selected.number, selected.mode), (1, "pending"))
        self.state["active_request"] = 0
        selected = select_request(self.root, self.state)
        self.assertEqual((selected.number, selected.mode), (0, "resume"))

    def test_template_only_request_requires_human_input(self) -> None:
        create_root = Path(tempfile.mkdtemp(dir=self.root))
        create_scaffold(create_root)
        state = load_state(create_root)
        with self.assertRaises(StoryPipelineError) as caught:
            select_request(create_root, state)
        self.assertEqual(caught.exception.exit_code, 8)

    def test_request_symlink_is_rejected(self) -> None:
        target = self.root / "outside.md"
        target.write_text("request\n")
        self.request.unlink()
        self.request.symlink_to(target)
        with self.assertRaises(StoryPipelineError):
            select_request(self.root, self.state)

    def test_interpretation_requires_exact_schema_and_explicit_targets(self) -> None:
        source = "`episodes/0001.md` を修正してください。"
        value = interpretation_value(kind="modify", targets=["episodes/0001.md"])
        parsed = parse_request_interpretation(json.dumps(value), source)
        self.assertEqual(parsed.targets, ("episodes/0001.md",))
        value["targets"] = ["episodes/0002.md"]
        with self.assertRaises(StoryPipelineError):
            parse_request_interpretation(json.dumps(value), source)
        value = interpretation_value(unknown=True)
        with self.assertRaises(StoryPipelineError):
            parse_request_interpretation(json.dumps(value), source)

    def test_decision_answers_must_exactly_match_pending_choices(self) -> None:
        self.state["pending_decisions"] = [
            {
                "id": "request-0000-decision-01",
                "request": 0,
                "question": "結末を変えるか",
                "reason": "要求と矛盾",
                "choices": ["維持", "変更"],
                "created_at": "2026-07-22T00:00:00Z",
            }
        ]
        value = interpretation_value(
            kind="answer",
            decision_answers=[{"id": "request-0000-decision-01", "answer": "変更"}],
        )
        parsed = parse_request_interpretation(json.dumps(value), "変更")
        resolutions = resolve_pending_decisions(self.state, parsed)
        self.assertEqual(resolutions[0].answer, "変更")
        value["decision_answers"] = []
        parsed = parse_request_interpretation(json.dumps(value), "変更")
        with self.assertRaises(StoryPipelineError) as caught:
            resolve_pending_decisions(self.state, parsed)
        self.assertEqual(caught.exception.exit_code, 8)

    def test_standard_scope_uses_first_missing_artifact(self) -> None:
        interpretation = parse_request_interpretation(
            json.dumps(interpretation_value()), self.request.read_text()
        )
        scope = determine_work_scope(self.root, self.state, interpretation)
        self.assertEqual(scope.action, "create_concept")
        for relative in ("concept.md", "world.md", "characters.md", "style.md", "canon.md"):
            (self.root / relative).write_text(f"# {relative}\n")
        scope = determine_work_scope(self.root, self.state, interpretation)
        self.assertEqual(scope.action, "create_plot")
        (self.root / "plot.md").write_text("# plot\n")
        (self.root / "chapters" / "0001.md").write_text("# chapter\n")
        scope = determine_work_scope(self.root, self.state, interpretation)
        self.assertEqual(scope.action, "create_episode_plan")

    def test_context_has_path_hash_boundaries_and_fixed_message_order(self) -> None:
        selected = select_request(self.root, self.state)
        messages = build_interpretation_messages(self.root, selected)
        digest = hashlib.sha256(self.request.read_bytes()).hexdigest()
        self.assertEqual([message["role"] for message in messages], ["system", "user", "user", "user"])
        self.assertIn(f"path=requests/0000.md sha256={digest}", messages[1]["content"])
        self.assertIn("BEGIN STORY DATA", messages[1]["content"])
        self.assertNotIn(self.request.read_text().strip(), messages[0]["content"])

    def test_context_rejects_symlink_even_inside_root(self) -> None:
        (self.root / "concept.md").write_text("concept\n")
        (self.root / "world.md").symlink_to(self.root / "concept.md")
        with self.assertRaises(StoryPipelineError):
            load_context_documents(self.root, ("world.md",))

    def test_mock_planner_integration_selects_scope(self) -> None:
        selected = select_request(self.root, self.state)

        class FakeClient:
            def complete_role(self, role: str, messages: list[dict[str, str]], **_: object) -> CompletionResult:
                self.role = role
                self.messages = messages
                response = ChatResponse(json.dumps(interpretation_value()), "mock", "stop")
                return CompletionResult(response, "mock", 1, ())

        client = FakeClient()
        planned = plan_selected_request(self.root, self.state, selected, client)
        self.assertEqual(client.role, "planner")
        self.assertEqual(planned.scope.action, "create_concept")
        self.assertEqual(planned.interpretation.kind, "continue")


if __name__ == "__main__":
    unittest.main()
