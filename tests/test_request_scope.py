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
from story_pipeline.llm_transport import ApiFailure, ChatResponse
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
        (self.root / ".story-pipeline" / "runs").mkdir(exist_ok=True)
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

    def test_standard_targets_are_normalized_but_mixed_explicit_targets_are_kept(self) -> None:
        for kind in ("create", "continue", "answer"):
            parsed = parse_request_interpretation(
                json.dumps(interpretation_value(kind=kind, targets=["concept.md"])),
                "短編を書いてください。",
            )
            self.assertEqual(parsed.targets, ())
        parsed = parse_request_interpretation(
            json.dumps(interpretation_value(kind="mixed", targets=["concept.md"])),
            "`concept.md` を修正した後に続きを書いてください。",
        )
        self.assertEqual(parsed.targets, ("concept.md",))

    def test_explicit_target_must_be_a_safe_path_from_request(self) -> None:
        value = interpretation_value(kind="modify", targets=["../concept.md"])
        with self.assertRaisesRegex(StoryPipelineError, "安全な作品ルート相対"):
            parse_request_interpretation(json.dumps(value), "`../concept.md` を修正")

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
            config = {"limits": {"generation_calls": 1}}

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
        self.assertEqual(planned.logical_calls, 1)

    def test_mock_planner_accepts_normalized_standard_target_without_regeneration(self) -> None:
        selected = select_request(self.root, self.state)

        class FakeClient:
            config = {"limits": {"generation_calls": 2}}

            def __init__(self) -> None:
                self.calls = 0

            def complete_role(self, _: str, messages: list[dict[str, str]], **__: object) -> CompletionResult:
                self.calls += 1
                value = interpretation_value(targets=["concept.md"])
                return CompletionResult(ChatResponse(json.dumps(value), "mock", "stop"), "mock", 1, ())

        client = FakeClient()
        planned = plan_selected_request(self.root, self.state, selected, client)
        self.assertEqual(client.calls, 1)
        self.assertEqual(planned.logical_calls, 1)
        self.assertEqual(planned.interpretation.targets, ())
        self.assertEqual(planned.scope.targets, ("concept.md",))

    def test_mock_planner_regenerates_invalid_api_response(self) -> None:
        selected = select_request(self.root, self.state)

        class FakeClient:
            config = {"limits": {"generation_calls": 2}}

            def __init__(self) -> None:
                self.calls = 0

            def complete_role(self, *_: object, **__: object) -> CompletionResult:
                self.calls += 1
                if self.calls == 1:
                    raise ApiFailure("invalid_response", "invalid")
                return CompletionResult(
                    ChatResponse(json.dumps(interpretation_value()), "mock", "stop"), "mock", 1, ()
                )

        client = FakeClient()
        planned = plan_selected_request(self.root, self.state, selected, client)
        self.assertEqual(client.calls, 2)
        self.assertEqual(planned.logical_calls, 2)


if __name__ == "__main__":
    unittest.main()
