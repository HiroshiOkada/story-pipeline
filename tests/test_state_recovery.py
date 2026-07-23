from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from story_pipeline.cli import main
from story_pipeline.scaffold import create_scaffold


class StateRecoveryTest(unittest.TestCase):
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

    def test_preserves_dirty_tree_then_recovers_invalid_state_for_new_request(self) -> None:
        for name in ("concept.md", "world.md", "characters.md", "style.md", "canon.md", "plot.md"):
            (self.root / name).write_text(f"# {name}\n")
        (self.root / "chapters/0001.md").write_text("# 第1章\n\n## 収録話\n0001\n")
        (self.root / "episodes/0001.md").write_text("# 本文\n")
        (self.root / "requests/0001.md").write_text("# 新しい要求\n\n続きを変更する\n")
        state_path = self.root / ".story-pipeline/state.json"
        state_path.write_text('{"phase": "broken", "active_request": 0}\n')

        code, stdout, stderr = self.run_cli("recover", "--abandon-active")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        recovered = json.loads(state_path.read_text())
        self.assertEqual(recovered["phase"], "chapter_revision")
        self.assertEqual(recovered["completed_episodes"], [1])
        self.assertEqual(recovered["completed_chapters"], [])
        self.assertIsNone(recovered["active_request"])
        self.assertEqual(recovered["pending_decisions"], [])
        subjects = self.git("log", "-2", "--format=%s").stdout.decode().splitlines()
        self.assertEqual(subjects, ["Recover story state", "Preserve worktree before recovery"])
        self.assertIn("Preserving worktree file: requests/0001.md", stdout)
        self.assertEqual(self.git("status", "--porcelain").stdout, b"")

    def test_salvages_valid_completed_chapter_prefix(self) -> None:
        for name in ("concept.md", "world.md", "characters.md", "style.md", "canon.md", "plot.md"):
            (self.root / name).write_text(f"# {name}\n")
        (self.root / "chapters/0001.md").write_text("# 第1章\n\n## 収録話\n0001\n")
        (self.root / "chapters/0002.md").write_text("# 第2章\n\n## 収録話\n0002\n")
        (self.root / "episodes/0001.md").write_text("# 本文\n")
        state_path = self.root / ".story-pipeline/state.json"
        state = json.loads(state_path.read_text())
        state.update({"completed_chapters": [1], "completed_episodes": [1], "active_request": 0})
        state_path.write_text(json.dumps(state, ensure_ascii=False) + "\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "Broken progress")

        code, _, _ = self.run_cli("recover", "--abandon-active")

        self.assertEqual(code, 0)
        recovered = json.loads(state_path.read_text())
        self.assertEqual(recovered["completed_chapters"], [1])
        self.assertEqual(recovered["phase"], "episode_planning")
        self.assertEqual(recovered["current_chapter"], 2)
        self.assertEqual(recovered["next_episode"], 2)

    def test_requires_explicit_abandon_without_changes(self) -> None:
        before = self.git("rev-parse", "HEAD").stdout

        code, _, stderr = self.run_cli("recover")

        self.assertEqual(code, 2)
        self.assertIn("--abandon-active", stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)

    def test_rejects_run_lock_without_preserving_or_changing_files(self) -> None:
        state_path = self.root / ".story-pipeline/state.json"
        before = state_path.read_bytes()
        (self.root / ".story-pipeline/run.lock").write_text("{}\n")

        code, _, _ = self.run_cli("recover", "--abandon-active")

        self.assertEqual(code, 5)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.decode().strip(), "Initial")

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("story_pipeline.project.Path.cwd", return_value=self.root),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
