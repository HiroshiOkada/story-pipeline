from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from story_pipeline.cli import main
from story_pipeline.config import load_config
from story_pipeline.drafting import DEFAULT_DRAFTING_CONTEXT
from story_pipeline.drafting_workflow import produce_draft
from story_pipeline.errors import StoryPipelineError
from story_pipeline.git_safety import inspect_run_preconditions
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ChatResponse, ChatTransport
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.run_lifecycle import create_run_record, finalize_run_record, resume_run_record
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state
from story_pipeline.workflow_executor import execute_planned_workflow


def _interpretation(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": "continue",
        "summary": "次の工程へ進める",
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


def _draft_payload() -> str:
    return json.dumps(
        {
            "path": "episodes/0001.md",
            "content": "## 話タイトル\n潮風\n\n## 本文\n" + "海" * 100 + "\n",
        },
        ensure_ascii=False,
    )


def _accepted_evaluation() -> str:
    return json.dumps(
        {
            "decision": "accept",
            "summary": "採用可能",
            "issues": [],
            "scores": {
                "request_fit": 5,
                "consistency": 5,
                "plan_fit": 5,
                "episode_completion": 5,
                "style_fit": 5,
                "readability": 5,
            },
        },
        ensure_ascii=False,
    )


class _FakeClient:
    config = {"limits": {"generation_calls": 3, "review_calls": 3, "revision_calls": 3}}

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)

    def complete_role(
        self, role: str, messages: list[dict[str, str]], **_: object
    ) -> CompletionResult:
        return CompletionResult(ChatResponse(next(self.responses), "mock", "stop"), "mock", 1, ())


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.body


class ProductionIncidentCharacterizationTest(unittest.TestCase):
    def test_i01_init_directly_followed_by_run_preflight_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(("init", str(root))), 0)

            preflight = inspect_run_preconditions(root, load_config(root))

            self.assertEqual(preflight.entries, ())

    def test_i02_inferred_standard_target_is_normalized_for_scope(self) -> None:
        response = json.dumps(
            _interpretation(kind="create", targets=["concept.md"]), ensure_ascii=False
        )
        interpretation = parse_request_interpretation(response, "短編を書いてください。")

        self.assertEqual(interpretation.targets, ())

    def test_i03_resume_keeps_obsolete_request_hash(self) -> None:
        original_hash = hashlib.sha256("最初の要求".encode()).hexdigest()
        changed_hash = hashlib.sha256("修正した要求".encode()).hexdigest()
        run = create_run_record(0, original_hash, "a" * 40)
        failed = finalize_run_record(
            run, "failed", resume_step="generate", resume_reason="再試行"
        )

        resumed = resume_run_record(failed, step="generate", reason="再試行")

        self.assertEqual(resumed["request_sha256"], original_hash)
        self.assertNotEqual(resumed["request_sha256"], changed_hash)

    def test_i05_validated_draft_is_not_saved_when_knowledge_extraction_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            (root / "requests/0000.md").write_text("# 要求\n\n第1話を書いてください。\n")
            for relative in DEFAULT_DRAFTING_CONTEXT:
                (root / relative).write_text(f"# {relative}\n\n採用済み\n")
            (root / "episode_plans/0001.md").write_text(
                "# 第1話\n\n## 目標文字数\n100字\n"
            )
            state = load_state(root)
            request = select_request(root, state)
            interpretation = parse_request_interpretation(
                json.dumps(_interpretation(), ensure_ascii=False), request.content
            )
            client = _FakeClient([_draft_payload(), _accepted_evaluation(), "{}", "{}"])

            result = produce_draft(root, request, interpretation, 1, client)

            self.assertEqual(result.status, "failed")
            self.assertIsNotNone(result.best)
            self.assertFalse((root / "episodes/0001.md").exists())

    def test_i07_last_episode_still_transitions_to_episode_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "chapters").mkdir()
            (root / "chapters/0001.md").write_text("## 収録話\n0001\n")
            candidate = SimpleNamespace(path="episodes/0001.md", content="本文")
            best = SimpleNamespace(candidate=candidate, evaluation=SimpleNamespace(summary="採用"))
            draft_result = SimpleNamespace(
                status="completed",
                best=best,
                candidates=(best,),
                calls=(),
                reason=None,
            )
            planned = SimpleNamespace(
                scope=SimpleNamespace(phase="drafting", targets=("episodes/0001.md",)),
                request=SimpleNamespace(),
                interpretation=SimpleNamespace(),
            )
            state = {
                "next_episode": 1,
                "completed_episodes": [],
                "current_chapter": 1,
            }
            with mock.patch(
                "story_pipeline.workflow_executor.produce_draft", return_value=draft_result
            ):
                result = execute_planned_workflow(root, state, planned, SimpleNamespace())

            self.assertEqual(result.state_updates["phase"], "episode_planning")
            self.assertEqual(result.state_updates["next_episode"], 2)

    def test_i09_transport_discards_provider_usage(self) -> None:
        body = json.dumps(
            {
                "model": "mock",
                "choices": [{"message": {"content": "本文"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            }
        ).encode()
        transport = ChatTransport(open_url=lambda *_args, **_kwargs: _Response(body))

        response = transport.complete(
            base_url="https://example.invalid/v1",
            api_key="secret",
            model="mock",
            messages=[],
            max_tokens=100,
            parameters={},
            timeout=1,
        )

        self.assertFalse(hasattr(response, "usage"))


if __name__ == "__main__":
    unittest.main()
