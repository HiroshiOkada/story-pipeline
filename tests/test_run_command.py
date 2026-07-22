from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from story_pipeline.concept import CONCEPT_HEADINGS
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ApiFailure, ChatResponse
from story_pipeline.run_command import run_command
from story_pipeline.run_start import prepare_run
from story_pipeline.scaffold import create_scaffold


def interpretation() -> str:
    return json.dumps({
        "kind": "continue",
        "summary": "短編の構想を作る",
        "targets": [],
        "required_conditions": [],
        "prohibited_changes": [],
        "additional_material": [],
        "decision_answers": [],
        "ambiguities": [],
        "requested_units": 1,
        "requested_until": None,
    }, ensure_ascii=False)


def concept(label: str = "採用案") -> str:
    return "\n\n".join(f"{heading}\n{label}" for heading in CONCEPT_HEADINGS) + "\n"


def evaluation(decision: str) -> str:
    return json.dumps({
        "decision": decision,
        "summary": "採用可能" if decision == "accept" else "人間の判断が必要",
        "issues": [],
        "scores": {"request_fit": 5, "consistency": 5},
    }, ensure_ascii=False)


class FakeClient:
    def __init__(self, config: dict[str, object], responses: list[str], *, probe_error: ApiFailure | None = None) -> None:
        self.config = config
        self.responses = iter(responses)
        self.probe_error = probe_error

    def probe_model(self, _: str) -> int:
        if self.probe_error is not None:
            raise self.probe_error
        return 1

    def complete_role(self, _: str, __: list[dict[str, str]], **___: object) -> CompletionResult:
        return CompletionResult(ChatResponse(next(self.responses), "mock-api", "stop"), "default", 1, ())


class RunCommandIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "Initial")
        (self.root / "requests/0000.md").write_text("# 要求\n\n短編を書いてください。\n", encoding="utf-8")
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)

    def test_success_commits_only_outputs_and_leaves_next_request_untracked(self) -> None:
        config = self._config()
        fake = FakeClient(config, [interpretation(), concept(), evaluation("accept")])
        output = io.StringIO()
        errors = io.StringIO()
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            code = run_command(output=output, error_output=errors)

        self.assertEqual(code, 0)
        self.assertTrue((self.root / "concept.md").is_file())
        self.assertTrue((self.root / "requests/0000_agent.md").is_file())
        self.assertTrue((self.root / ".story-pipeline/runs/0000.json").is_file())
        run = json.loads((self.root / ".story-pipeline/runs/0000.json").read_text())
        self.assertEqual(run["status"], "completed")
        self.assertEqual(json.loads((self.root / ".story-pipeline/state.json").read_text())["phase"], "foundation")
        self.assertIn("Models: planner=gpt-4.1", output.getvalue())
        self.assertEqual(errors.getvalue(), "")
        status = self.git("status", "--short").stdout.decode()
        self.assertEqual(status, "?? requests/0001.md\n")
        subjects = self.git("log", "-2", "--format=%s").stdout.decode().splitlines()
        self.assertEqual(subjects, ["Complete request 0000: completed", "Record request 0000 input"])

    def test_awaiting_human_writes_report_without_adopting_candidate(self) -> None:
        fake = FakeClient(self._config(), [interpretation(), concept(), evaluation("awaiting_human")])
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            code = run_command(output=io.StringIO(), error_output=io.StringIO())
        self.assertEqual(code, 8)
        self.assertFalse((self.root / "concept.md").exists())
        run = json.loads((self.root / ".story-pipeline/runs/0000.json").read_text())
        self.assertEqual(run["status"], "awaiting_human")
        self.assertTrue((self.root / "requests/0000_agent.md").is_file())

    def test_failed_workflow_persists_resume_information(self) -> None:
        fake = FakeClient(self._config(), [interpretation(), "invalid", "invalid", "invalid"])
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            code = run_command(output=io.StringIO(), error_output=io.StringIO())
        self.assertEqual(code, 7)
        run = json.loads((self.root / ".story-pipeline/runs/0000.json").read_text())
        state = json.loads((self.root / ".story-pipeline/state.json").read_text())
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["resume"]["step"], "generate")
        self.assertEqual(state["active_request"], 0)
        self.assertFalse((self.root / ".story-pipeline/run.lock").exists())

    def test_i08_unexpected_workflow_error_becomes_generic_exit_9(self) -> None:
        class BrokenClient(FakeClient):
            def complete_role(self, *_: object, **__: object) -> CompletionResult:
                raise RuntimeError("保存してはいけない内部情報")

        fake = BrokenClient(self._config(), [])
        errors = io.StringIO()
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            code = run_command(output=io.StringIO(), error_output=errors)

        run = json.loads((self.root / ".story-pipeline/runs/0000.json").read_text())
        self.assertEqual(code, 9)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["errors"][-1]["message"], "予期しない内部エラーが発生しました")
        self.assertNotIn("RuntimeError", json.dumps(run, ensure_ascii=False))
        self.assertNotIn("保存してはいけない", errors.getvalue())
        self.assertFalse((self.root / ".story-pipeline/run.lock").exists())

    def test_connection_failure_preserves_human_input_and_releases_lock(self) -> None:
        failure = ApiFailure("authentication", "認証失敗", 401)
        fake = FakeClient(self._config(), [], probe_error=failure)
        before = self.git("rev-parse", "HEAD").stdout
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            with self.assertRaises(ApiFailure):
                prepare_run(output=io.StringIO())
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)
        self.assertEqual((self.root / "requests/0000.md").read_text(), "# 要求\n\n短編を書いてください。\n")
        self.assertFalse((self.root / ".story-pipeline/run.lock").exists())
        self.assertFalse((self.root / ".story-pipeline/runs/0000.json").exists())

    def test_no_pending_request_has_no_side_effect(self) -> None:
        with patch("story_pipeline.run_command.prepare_run", return_value=None):
            output = io.StringIO()
            code = run_command(output=output, error_output=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "No pending request.\n")

    def _config(self) -> dict[str, object]:
        from story_pipeline.config import load_config
        return load_config(self.root)

    def git(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


if __name__ == "__main__":
    unittest.main()
