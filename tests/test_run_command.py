from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from story_pipeline.concept import CONCEPT_HEADINGS
from story_pipeline.cli import main
from story_pipeline.config import load_config
from story_pipeline.llm_client import CompletionResult
from story_pipeline.llm_transport import ApiFailure, ChatResponse
from story_pipeline.errors import StoryPipelineError
from story_pipeline.interruptions import TerminationSignal
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


def draft() -> str:
    return json.dumps({
        "path": "episodes/0001.md",
        "title": "潮風", "body": "二人は古い看板を直し始めた。" + "海" * 72 + "。",
    }, ensure_ascii=False)


def draft_evaluation() -> str:
    return json.dumps({
        "decision": "accept", "summary": "採用可能", "issues": [],
        "scores": {
            "request_fit": 5, "consistency": 5, "plan_fit": 5,
            "episode_completion": 5, "style_fit": 5, "readability": 5,
        },
    }, ensure_ascii=False)


def draft_knowledge() -> str:
    return json.dumps({
        "canon_facts": [{
            "fact": "二人が看板を直し始めた", "evidence": "二人は古い看板を直し始めた。",
            "source": "episodes/0001.md", "established_at": "第1話", "people": ["二人"],
        }],
        "character_states": [{
            "character": "二人", "state": "共同作業中", "evidence": "二人は古い看板を直し始めた。",
            "source": "episodes/0001.md", "established_at": "第1話",
        }],
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


class InitializedRunCommandIntegrationTest(unittest.TestCase):
    def test_init_request_edit_and_fake_run_adopt_concept(self) -> None:
        identity = {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(os.environ, identity):
            root = Path(temporary_directory)
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(("init", str(root))), 0)
            (root / "requests/0000.md").write_text(
                "# 要求\n\n短編を書いてください。\n", encoding="utf-8"
            )
            inferred = json.loads(interpretation())
            inferred["kind"] = "create"
            inferred["targets"] = ["concept.md"]
            fake = FakeClient(
                load_config(root),
                [json.dumps(inferred, ensure_ascii=False), concept(), evaluation("accept")],
            )
            previous = Path.cwd()
            os.chdir(root)
            try:
                with patch("story_pipeline.run_start.LLMClient", return_value=fake):
                    code = run_command(output=io.StringIO(), error_output=io.StringIO())
            finally:
                os.chdir(previous)

            self.assertEqual(code, 0)
            self.assertTrue((root / "concept.md").is_file())
            subjects = subprocess.run(
                ["git", "-C", str(root), "log", "-3", "--format=%s"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(
                subjects,
                [
                    "Complete request 0000: completed",
                    "Record request 0000 input",
                    "Initialize story project",
                ],
            )


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

    def test_drafting_adopts_episode_canon_characters_and_checkpoint_together(self) -> None:
        state_path = self.root / ".story-pipeline/state.json"
        state = json.loads(state_path.read_text())
        state["phase"] = "drafting"
        state["current_chapter"] = 1
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        documents = {
            "concept.md": "# 構想\n",
            "world.md": "# 世界\n",
            "characters.md": "## 人物一覧\n二人\n\n## 人物別の目的・変化・口調・状態\n開始前\n",
            "plot.md": "# 構成\n",
            "style.md": "# 文体\n",
            "canon.md": "## 確定事実\n開始前\n\n## 人物状態\n開始前\n",
            "chapters/0001.md": "# 第1章\n\n## 収録話\n0001\n",
            "episode_plans/0001.md": "# 第1話\n\n## 目標文字数\n100字\n",
        }
        for relative, content in documents.items():
            (self.root / relative).write_text(content, encoding="utf-8")
        self.git("add", ".story-pipeline/state.json", "requests/0000.md", *documents)
        self.git("commit", "-q", "-m", "Prepare drafting fixture")
        fake = FakeClient(
            self._config(), [interpretation(), draft(), draft_evaluation(), draft_knowledge()]
        )

        output = io.StringIO()
        errors = io.StringIO()
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            code = run_command(output=output, error_output=errors)

        self.assertEqual(code, 0, errors.getvalue())
        self.assertTrue((self.root / "episodes/0001.md").is_file())
        self.assertIn("二人が看板を直し始めた", (self.root / "canon.md").read_text())
        self.assertIn("共同作業中", (self.root / "characters.md").read_text())
        checkpoint = json.loads(
            (self.root / ".story-pipeline/checkpoints/0000/draft.json").read_text()
        )
        self.assertEqual(checkpoint["adoption"]["status"], "adopted")
        run = self._run_record()
        self.assertEqual(run["call_counts"]["knowledge"], 1)

    def test_unchanged_failed_run_resume_keeps_single_revision(self) -> None:
        self._create_failed_run()

        self._resume_successfully()

        run = self._run_record()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["resume_count"], 1)
        self.assertEqual(len(run["request_revisions"]), 1)

    def test_uncommitted_request_revision_is_committed_and_recorded(self) -> None:
        self._create_failed_run()
        initial = self._run_record()
        revised = "# 要求\n\n短編の舞台を港町にしてください。\n"
        (self.root / "requests/0000.md").write_text(revised, encoding="utf-8")

        self._resume_successfully()

        run = self._run_record()
        revisions = run["request_revisions"]
        self.assertEqual(run["request_sha256"], hashlib.sha256(revised.encode()).hexdigest())
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]["input_commit"], run["start_commit"])
        self.assertEqual(
            revisions[1]["input_commit"],
            self.git("rev-parse", "HEAD~1").stdout.decode().strip(),
        )
        self.assertEqual(revisions[1]["applies_from_step"], "interpret_request")
        subjects = self.git("log", "-3", "--format=%s").stdout.decode().splitlines()
        self.assertIn("Record request 0000 input", subjects)

    def test_committed_request_revision_uses_existing_head_boundary(self) -> None:
        self._create_failed_run()
        revised = "# 要求\n\n短編の舞台を山村にしてください。\n"
        (self.root / "requests/0000.md").write_text(revised, encoding="utf-8")
        self.git("add", "requests/0000.md")
        self.git("commit", "-q", "-m", "Revise request")
        revision_commit = self.git("rev-parse", "HEAD").stdout.decode().strip()

        self._resume_successfully()

        run = self._run_record()
        self.assertEqual(run["request_revisions"][-1]["input_commit"], revision_commit)
        subjects = self.git("log", "-3", "--format=%s").stdout.decode().splitlines()
        self.assertEqual(subjects.count("Record request 0000 input"), 0)

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
        self.assertEqual(run["incidents"][-1]["exception_class"], "RuntimeError")
        self.assertEqual(run["incidents"][-1]["component"], "workflow")
        self.assertNotIn("保存してはいけない", errors.getvalue())
        self.assertFalse((self.root / ".story-pipeline/run.lock").exists())

    def test_report_failure_records_finalizing_incident(self) -> None:
        fake = FakeClient(self._config(), [interpretation(), concept(), evaluation("accept")])
        errors = io.StringIO()
        with (
            patch("story_pipeline.run_start.LLMClient", return_value=fake),
            patch("story_pipeline.run_command._write_report", side_effect=OSError("secret path")),
        ):
            code = run_command(output=io.StringIO(), error_output=errors)

        run = self._run_record()
        self.assertEqual(code, 9)
        self.assertEqual(run["incidents"][-1]["component"], "finalizing")
        self.assertEqual(run["incidents"][-1]["exception_class"], "OSError")
        self.assertNotIn("secret path", errors.getvalue())

    def test_git_failure_records_git_incident(self) -> None:
        fake = FakeClient(self._config(), [interpretation(), concept(), evaluation("accept")])
        with (
            patch("story_pipeline.run_start.LLMClient", return_value=fake),
            patch("story_pipeline.run_command.commit_run_outputs", side_effect=RuntimeError("git secret")),
        ):
            code = run_command(output=io.StringIO(), error_output=io.StringIO())

        run = self._run_record()
        self.assertEqual(code, 9)
        self.assertEqual(run["incidents"][-1]["component"], "git")
        self.assertFalse(run["incidents"][-1]["retryable"])

    def test_safe_git_error_reason_is_persisted_without_traceback(self) -> None:
        fake = FakeClient(self._config(), [interpretation(), concept(), evaluation("accept")])
        failure = StoryPipelineError("指定ファイルを stage できません", "safe", "check", 5)
        with (
            patch("story_pipeline.run_start.LLMClient", return_value=fake),
            patch("story_pipeline.run_command.commit_run_outputs", side_effect=failure),
        ):
            code = run_command(output=io.StringIO(), error_output=io.StringIO())

        run = self._run_record()
        self.assertEqual(code, 9)
        self.assertEqual(run["errors"][-1]["category"], "git")
        self.assertEqual(run["errors"][-1]["message"], failure.reason)

    def test_sigint_is_recorded_as_interruption(self) -> None:
        self._assert_interruption(KeyboardInterrupt(), 130)

    def test_sigterm_is_distinct_from_sigint(self) -> None:
        self._assert_interruption(TerminationSignal(), 143)

    def test_lock_release_failure_is_a_secondary_incident(self) -> None:
        fake = FakeClient(self._config(), [interpretation(), concept(), evaluation("accept")])
        with (
            patch("story_pipeline.run_start.LLMClient", return_value=fake),
            patch("story_pipeline.run_lock.RunLock.release", side_effect=OSError("lock secret")),
        ):
            code = run_command(output=io.StringIO(), error_output=io.StringIO())

        incident = self._run_record()["incidents"][-1]
        self.assertEqual(code, 9)
        self.assertEqual(incident["component"], "lock")
        self.assertEqual(incident["step"], "release_lock")
        (self.root / ".story-pipeline/run.lock").unlink(missing_ok=True)

    def _assert_interruption(self, error: BaseException, expected_code: int) -> None:
        class InterruptedClient(FakeClient):
            def complete_role(self, *_: object, **__: object) -> CompletionResult:
                raise error

        fake = InterruptedClient(self._config(), [])
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            code = run_command(output=io.StringIO(), error_output=io.StringIO())

        incident = self._run_record()["incidents"][-1]
        self.assertEqual(code, expected_code)
        self.assertEqual(incident["component"], "interruption")
        self.assertEqual(incident["exception_class"], type(error).__name__)

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

    def _create_failed_run(self) -> None:
        fake = FakeClient(self._config(), [interpretation(), "invalid", "invalid", "invalid"])
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            self.assertEqual(run_command(output=io.StringIO(), error_output=io.StringIO()), 7)

    def _resume_successfully(self) -> None:
        fake = FakeClient(self._config(), [interpretation(), concept(), evaluation("accept")])
        with patch("story_pipeline.run_start.LLMClient", return_value=fake):
            self.assertEqual(run_command(output=io.StringIO(), error_output=io.StringIO()), 0)

    def _run_record(self) -> dict[str, object]:
        return json.loads((self.root / ".story-pipeline/runs/0000.json").read_text())

    def git(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


if __name__ == "__main__":
    unittest.main()
