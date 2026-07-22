from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from story_pipeline.cli import main
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state
from story_pipeline.status import determine_next_action, inspect_status


class StatusCommandTest(unittest.TestCase):
    def invoke_at(self, root: Path) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("story_pipeline.project.Path.cwd", return_value=root),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(("status",))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_scaffold_status_matches_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)

            code, stdout, stderr = self.invoke_at(root)

            self.assertEqual(code, 0)
            self.assertIn(f"Root: {root.resolve()}\n", stdout)
            self.assertIn("Phase: concept\n", stdout)
            self.assertIn("Last request: none\n", stdout)
            self.assertIn("Active request: none\n", stdout)
            self.assertIn("Current chapter: none\n", stdout)
            self.assertIn("Next episode: 0001\n", stdout)
            self.assertIn("Next action: create concept\n", stdout)
            self.assertEqual(stderr, "")

    def test_reports_last_request_status_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            state_path = root / ".story-pipeline" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["last_request"] = 0
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (root / "requests" / "0000_agent.md").write_text("# report\n", encoding="utf-8")
            runs = root / ".story-pipeline" / "runs"
            runs.mkdir(exist_ok=True)
            (runs / "0000.json").write_text(
                json.dumps({"status": "completed"}), encoding="utf-8"
            )
            (root / ".story-pipeline" / "run.lock").write_text(
                json.dumps({"pid": 123, "hostname": "example.test"}), encoding="utf-8"
            )

            code, stdout, stderr = self.invoke_at(root)

            self.assertEqual(code, 0)
            self.assertIn("Last request: 0000 (completed)\n", stdout)
            self.assertIn("Lock: pid=123, hostname=example.test\n", stdout)
            self.assertEqual(stderr, "")

    def test_warns_about_state_and_file_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            state_path = root / ".story-pipeline" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["phase"] = "drafting"
            state["completed_episodes"] = [1]
            state["next_episode"] = 2
            state_path.write_text(json.dumps(state), encoding="utf-8")

            code, stdout, stderr = self.invoke_at(root)

            self.assertEqual(code, 0)
            self.assertIn("Completed episodes: 1\n", stdout)
            self.assertIn("Warning: COMPLETED_FILE_MISSING", stderr)
            self.assertIn("Warning: PHASE_ARTIFACT_MISSING", stderr)

    def test_status_does_not_modify_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            before = self.file_contents(root)

            code, _, _ = self.invoke_at(root)

            self.assertEqual(code, 0)
            self.assertEqual(self.file_contents(root), before)

    def test_warns_when_completed_chapter_needs_revision_or_next_episode_is_wrong(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            (root / "chapters/0001.md").write_text("## 収録話\n0001-0002\n")
            state = load_state(root)
            state.update({
                "phase": "episode_planning",
                "current_chapter": 1,
                "next_chapter": 1,
                "next_episode": 2,
                "completed_episodes": [1, 2],
            })

            snapshot = inspect_status(root, state)

            warning = next(item for item in snapshot.warnings if item.code == "CHAPTER_REVISION_REQUIRED")
            self.assertEqual(warning.location, "/phase")

    def test_warns_with_chapter_location_for_invalid_story_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            (root / "chapters/0001.md").write_text("## 収録話\n0001 0003\n")

            snapshot = inspect_status(root, load_state(root))

            warning = next(item for item in snapshot.warnings if item.code == "STORY_STRUCTURE_INVALID")
            self.assertEqual(warning.location, "chapters/0001.md ## 収録話")

    def test_rejects_config_symlink_outside_search_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            child = root / "child"
            child.mkdir()
            (child / "story-pipeline-config.jsonc").symlink_to(
                root / "story-pipeline-config.jsonc"
            )

            code, stdout, stderr = self.invoke_at(child)

            self.assertEqual(code, 4)
            self.assertEqual(stdout, "")
            self.assertIn("探索対象ディレクトリ外", stderr)

    @staticmethod
    def file_contents(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }


class NextActionTest(unittest.TestCase):
    def test_drafting_uses_existing_episode_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            state = load_state(root)
            state["phase"] = "drafting"
            self.assertEqual(determine_next_action(root, state), "create episode plan 0001")
            (root / "episode_plans" / "0001.md").write_text("# plan\n", encoding="utf-8")
            self.assertEqual(determine_next_action(root, state), "draft episode 0001")

    def test_pending_decision_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            state = load_state(root)
            state["pending_decisions"] = [{"id": "request-0000-decision-01"}]
            self.assertEqual(
                determine_next_action(root, state),
                "answer decision request-0000-decision-01",
            )


if __name__ == "__main__":
    unittest.main()
