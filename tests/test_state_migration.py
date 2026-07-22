from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from story_pipeline.cli import main
from story_pipeline.scaffold import create_scaffold


class StateMigrationTest(unittest.TestCase):
    def test_three_incident_fixtures_migrate_to_chapter_revision_without_touching_story(self) -> None:
        for fixture in ("orbital", "gravity", "memory"):
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                create_scaffold(root)
                self.git(root, "init", "-q")
                self.git(root, "config", "user.name", "Test")
                self.git(root, "config", "user.email", "test@example.invalid")
                (root / "chapters/0001.md").write_text("# 第1章\n\n## 収録話\n0001\n")
                episode = root / "episodes/0001.md"
                episode.write_text(f"## 話タイトル\n{fixture}\n\n## 本文\n完成本文\n")
                state_path = root / ".story-pipeline/state.json"
                state = json.loads(state_path.read_text())
                state.update({
                    "phase": "episode_planning", "current_chapter": 1,
                    "next_chapter": 1, "next_episode": 2, "completed_episodes": [1],
                })
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                self.git(root, "add", ".")
                self.git(root, "commit", "-q", "-m", "Incident fixture")
                before = hashlib.sha256(episode.read_bytes()).hexdigest()

                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch("story_pipeline.project.Path.cwd", return_value=root),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = main(("migrate-state",))

                migrated = json.loads(state_path.read_text())
                self.assertEqual(code, 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(migrated["phase"], "chapter_revision")
                self.assertEqual(migrated["next_episode"], 2)
                self.assertEqual(hashlib.sha256(episode.read_bytes()).hexdigest(), before)
                self.assertEqual(
                    self.git(root, "log", "-1", "--format=%s").stdout.decode().strip(),
                    "Migrate story state",
                )

    def test_rejects_dirty_worktree_without_modifying_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Test")
            self.git(root, "config", "user.email", "test@example.invalid")
            self.git(root, "add", ".")
            self.git(root, "commit", "-q", "-m", "Initial")
            state_path = root / ".story-pipeline/state.json"
            before = state_path.read_bytes()
            (root / "notes.txt").write_text("dirty\n")

            with (
                patch("story_pipeline.project.Path.cwd", return_value=root),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = main(("migrate-state",))

            self.assertEqual(code, 5)
            self.assertEqual(state_path.read_bytes(), before)

    def test_rejects_run_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            (root / ".story-pipeline/run.lock").write_text("{}\n")
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Test")
            self.git(root, "config", "user.email", "test@example.invalid")
            self.git(root, "add", ".")
            self.git(root, "commit", "-q", "-m", "Initial")

            with (
                patch("story_pipeline.project.Path.cwd", return_value=root),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = main(("migrate-state",))

            self.assertEqual(code, 5)

    @staticmethod
    def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(root), *arguments], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
