from __future__ import annotations

import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from story_pipeline.errors import StoryPipelineError
from story_pipeline.git_safety import (
    commit_explicit_paths,
    commit_run_outputs,
    inspect_run_preconditions,
    restore_managed_files,
)
from story_pipeline.git_validation import classify_path, read_worktree
from story_pipeline.run_lock import RunLock


class GitSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".story-pipeline").mkdir()
        (self.root / "requests").mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("story-pipeline-config.jsonc", "{}\n")
        self.write("requests/0000.md", "request\n")
        self.write("concept.md", "original\n")
        self.git("add", "story-pipeline-config.jsonc", "requests/0000.md", "concept.md")
        self.git("commit", "-q", "-m", "Initial")
        self.config = {"dotenv": {"files": []}}

    def test_path_classification_uses_full_match(self) -> None:
        self.assertEqual(classify_path("episodes/0001.md"), "managed")
        self.assertEqual(classify_path("episodes/00001.md"), "unexpected")
        self.assertEqual(classify_path("episodes/0001.md.bak"), "unexpected")
        self.assertEqual(classify_path("requests/0001.md"), "human")
        self.assertEqual(classify_path(".story-pipeline/run.lock"), "temporary")

    def test_porcelain_parser_includes_rename_source_and_destination(self) -> None:
        self.git("mv", "concept.md", "world.md")

        entries = read_worktree(self.root)

        paths = {(entry.path, entry.rename_origin) for entry in entries}
        self.assertIn(("world.md", False), paths)
        self.assertIn(("concept.md", True), paths)

    def test_preflight_rejects_staged_and_untracked_managed_files(self) -> None:
        self.write("story-pipeline-config.jsonc", '{"changed": true}\n')
        self.git("add", "story-pipeline-config.jsonc")
        with self.assertRaisesRegex(StoryPipelineError, "stage 済み"):
            inspect_run_preconditions(self.root, self.config)
        self.git("restore", "--staged", "story-pipeline-config.jsonc")
        self.write("episodes/0001.md", "episode\n")
        with self.assertRaisesRegex(StoryPipelineError, "未追跡"):
            inspect_run_preconditions(self.root, self.config)

    def test_preflight_rejects_index_flag_on_clean_managed_file(self) -> None:
        self.git("update-index", "--skip-worktree", "concept.md")

        with self.assertRaisesRegex(StoryPipelineError, "index flag"):
            inspect_run_preconditions(self.root, self.config)

    def test_restore_changes_only_tracked_managed_files(self) -> None:
        self.write("concept.md", "direct edit\n")
        self.write("requests/0000.md", "human edit\n")
        self.write("notes.txt", "unknown\n")
        preflight = inspect_run_preconditions(self.root, self.config)
        output = io.StringIO()

        restored = restore_managed_files(self.root, preflight, output)

        self.assertEqual(restored, ("concept.md",))
        self.assertEqual((self.root / "concept.md").read_text(), "original\n")
        self.assertEqual((self.root / "requests/0000.md").read_text(), "human edit\n")
        self.assertEqual((self.root / "notes.txt").read_text(), "unknown\n")
        self.assertEqual(output.getvalue(), "Restoring managed file: concept.md\n")

    def test_explicit_commit_does_not_include_unknown_file(self) -> None:
        self.write("requests/0000.md", "updated request\n")
        self.write("notes.txt", "keep untracked\n")

        commit = commit_explicit_paths(
            self.root, ("requests/0000.md",), "Record request 0000 input"
        )

        self.assertEqual(commit, self.git("rev-parse", "HEAD").stdout.decode().strip())
        self.assertEqual(self.git("show", "--format=", "--name-only", "HEAD").stdout.decode().strip(), "requests/0000.md")
        self.assertIn("?? notes.txt", self.git("status", "--short").stdout.decode())

    def test_commit_requires_exact_changed_path_set(self) -> None:
        self.write("requests/0000.md", "updated request\n")
        with self.assertRaisesRegex(StoryPipelineError, "予定集合"):
            commit_explicit_paths(
                self.root,
                ("requests/0000.md", "story-pipeline-config.jsonc"),
                "Record request 0000 input",
            )
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout, b"")

    def test_output_commit_rejects_non_managed_path(self) -> None:
        with self.assertRaisesRegex(StoryPipelineError, "管理対象外"):
            commit_run_outputs(self.root, 0, "completed", ("requests/0000.md",))

    def test_lock_acquire_update_and_release(self) -> None:
        lock = RunLock.acquire(self.root)
        self.assertTrue(lock.path.exists())
        lock.update_request(7)
        value = json.loads(lock.path.read_text(encoding="utf-8"))
        self.assertEqual(value["request_number"], 7)
        lock.release()
        self.assertFalse(lock.path.exists())

    def test_live_and_broken_locks_are_preserved(self) -> None:
        lock = RunLock.acquire(self.root)
        with self.assertRaises(StoryPipelineError) as caught:
            RunLock.acquire(self.root)
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertTrue(lock.path.exists())
        lock.release()
        lock.path.write_text("broken json\n", encoding="utf-8")
        with self.assertRaises(StoryPipelineError) as caught:
            RunLock.acquire(self.root)
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertEqual(lock.path.read_text(), "broken json\n")

    def test_dead_process_with_running_record_is_reported_as_stale(self) -> None:
        runs = self.root / ".story-pipeline" / "runs"
        runs.mkdir()
        (runs / "0003.json").write_text(
            json.dumps({"request_number": 3, "status": "running"}), encoding="utf-8"
        )
        lock_path = self.root / ".story-pipeline" / "run.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pid": 2147483647,
                    "hostname": socket.gethostname(),
                    "started_at": "2026-01-01T00:00:00Z",
                    "request_number": 3,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(StoryPipelineError, "停止した実行") as caught:
            RunLock.acquire(self.root)
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertTrue(lock_path.exists())

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


if __name__ == "__main__":
    unittest.main()
