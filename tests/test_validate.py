from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from story_pipeline.cli import main
from story_pipeline.scaffold import create_scaffold


class ValidateCommandTest(unittest.TestCase):
    def invoke_at(
        self, root: Path, environment: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        process_environment = {} if environment is None else environment
        with (
            mock.patch("story_pipeline.project.Path.cwd", return_value=root),
            mock.patch.dict(os.environ, process_environment, clear=True),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(("validate",))
        return code, stdout.getvalue(), stderr.getvalue()

    def create_valid_project(self, root: Path) -> None:
        create_scaffold(root)
        config_path = root / "story-pipeline-config.jsonc"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["dotenv"]["files"] = [".env"]
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        (root / ".env").write_text("OPENAI_API_KEY=test-only-value\n", encoding="utf-8")
        self.git(root, "init", "-q")
        self.git(
            root,
            "add",
            ".gitignore",
            "story-pipeline-config.jsonc",
            "requests/0000.md",
            ".story-pipeline/state.json",
        )
        self.git(
            root,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "Initial project",
        )

    def test_valid_project_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)

            code, stdout, stderr = self.invoke_at(root)

            self.assertEqual(code, 0)
            self.assertEqual(stdout, "Validation passed.\n")
            self.assertEqual(stderr, "")

    def test_unknown_untracked_file_is_warning_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)
            (root / "notes.txt").write_text("memo\n", encoding="utf-8")

            code, stdout, _ = self.invoke_at(root)

            self.assertEqual(code, 0)
            self.assertIn("WARNING UNTRACKED_UNKNOWN_FILE", stdout)
            self.assertIn("Validation passed with 1 warning(s).", stdout)

    def test_untracked_managed_file_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)
            (root / "episodes" / "0001.md").write_text("# episode\n", encoding="utf-8")

            code, stdout, _ = self.invoke_at(root)

            self.assertEqual(code, 4)
            self.assertIn("ERROR UNTRACKED_MANAGED_FILE", stdout)
            self.assertIn("Validation failed:", stdout)

    def test_continues_after_invalid_config_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)
            (root / "story-pipeline-config.jsonc").write_text('{"unknown": true}\n', encoding="utf-8")
            (root / ".story-pipeline" / "state.json").write_text("not json\n", encoding="utf-8")

            code, stdout, _ = self.invoke_at(root)

            self.assertEqual(code, 4)
            self.assertIn("ERROR CONFIG_INVALID", stdout)
            self.assertIn("ERROR STATE_INVALID", stdout)
            self.assertIn("ERROR MODIFIED_MANAGED_FILE", stdout)

    def test_staged_change_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)
            config_path = root / "story-pipeline-config.jsonc"
            config_path.write_text(config_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.git(root, "add", "story-pipeline-config.jsonc")

            code, stdout, _ = self.invoke_at(root)

            self.assertEqual(code, 4)
            self.assertIn("ERROR GIT_STAGED_CHANGE", stdout)

    def test_detects_request_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)
            request = root / "requests" / "0000.md"
            request_hash = hashlib.sha256(request.read_bytes()).hexdigest()
            state_path = root / ".story-pipeline" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["last_request"] = 0
            state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            (root / "requests" / "0000_agent.md").write_text("# report\n", encoding="utf-8")
            runs = root / ".story-pipeline" / "runs"
            runs.mkdir(exist_ok=True)
            run = self.completed_run(request_hash)
            (runs / "0000.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
            self.git(
                root,
                "add",
                ".story-pipeline/state.json",
                ".story-pipeline/runs/0000.json",
                "requests/0000_agent.md",
            )
            self.git(
                root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "-m",
                "Complete request",
            )
            request.write_text(request.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")

            code, stdout, _ = self.invoke_at(root)

            self.assertEqual(code, 4)
            self.assertIn("ERROR REQUEST_HASH_MISMATCH", stdout)

    def test_missing_api_key_is_reported_without_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)
            (root / ".env").write_text("", encoding="utf-8")

            code, stdout, _ = self.invoke_at(root)

            self.assertEqual(code, 4)
            self.assertIn("ERROR API_KEY_ENV_MISSING", stdout)
            self.assertNotIn("test-only-value", stdout)

    def test_configured_project_dotenv_must_be_ignored_and_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)
            config_path = root / "story-pipeline-config.jsonc"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["dotenv"]["files"] = ["secrets.env"]
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            (root / "secrets.env").write_text("OPENAI_API_KEY=secret-value\n", encoding="utf-8")
            self.git(root, "add", "story-pipeline-config.jsonc", "secrets.env")
            self.git(
                root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "-m",
                "Track unsafe dotenv",
            )

            code, stdout, _ = self.invoke_at(root)

            self.assertEqual(code, 4)
            self.assertIn("ERROR GITIGNORE_REQUIRED_PATTERN", stdout)
            self.assertIn("ERROR TRACKED_TEMPORARY_FILE", stdout)
            self.assertNotIn("secret-value", stdout)

    def test_validate_does_not_modify_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.create_valid_project(root)
            before_files = self.file_contents(root)
            before_status = self.git(root, "status", "--porcelain=v2", "-z").stdout

            code, _, _ = self.invoke_at(root)

            after_status = self.git(root, "status", "--porcelain=v2", "-z").stdout
            self.assertEqual(code, 0)
            self.assertEqual(self.file_contents(root), before_files)
            self.assertEqual(after_status, before_status)

    @staticmethod
    def completed_run(request_hash: str) -> dict[str, object]:
        timestamp = "2026-07-22T01:23:45Z"
        return {
            "schema_version": 1,
            "request_number": 0,
            "status": "completed",
            "started_at": timestamp,
            "updated_at": timestamp,
            "finished_at": timestamp,
            "request_sha256": request_hash,
            "start_commit": "a" * 40,
            "end_commit": None,
            "current_step": "report",
            "steps": [],
            "call_counts": {"generation": 0, "review": 0, "revision": 0, "summary": 0},
            "model_attempts": [],
            "input_hashes": {},
            "output_hashes": {},
            "restored_files": [],
            "fallbacks": [],
            "errors": [],
            "resume": None,
        }

    @staticmethod
    def file_contents(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if ".git" not in path.relative_to(root).parts and path.is_file()
        }

    @staticmethod
    def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


if __name__ == "__main__":
    unittest.main()
