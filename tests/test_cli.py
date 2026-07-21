from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from story_pipeline import __version__
from story_pipeline.cli import main
from story_pipeline.scaffold import create_scaffold


class CliTest(unittest.TestCase):
    def invoke(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_no_arguments_prints_help(self) -> None:
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("usage: story-pipeline", stdout)
        self.assertEqual(stderr, "")

    def test_version_prints_package_version(self) -> None:
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with contextlib.redirect_stdout(stdout):
                main(("--version",))
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), f"story-pipeline {__version__}\n")

    def test_init_rejects_dash_as_usage_error(self) -> None:
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with contextlib.redirect_stderr(stderr):
                main(("init", "-"))
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("PATH に '-' は指定できません", stderr.getvalue())

    def test_init_creates_scaffold_and_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            code, stdout, stderr = self.invoke("init", str(root))

            self.assertEqual(code, 0)
            self.assertIn(str(root.resolve()), stdout)
            self.assertEqual(stderr, "")
            expected = {
                ".git",
                ".gitignore",
                ".story-pipeline",
                "chapters",
                "episode_plans",
                "episodes",
                "requests",
                "story-pipeline-config.jsonc",
            }
            self.assertEqual({path.name for path in root.iterdir()}, expected)
            self.assertTrue((root / "requests" / "0000.md").is_file())
            state = json.loads((root / ".story-pipeline" / "state.json").read_text())
            self.assertEqual(state["schema_version"], 1)
            self.assertEqual(state["phase"], "concept")

    def test_init_rejects_nonempty_directory_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            existing = root / "notes.txt"
            existing.write_text("keep", encoding="utf-8")

            code, stdout, stderr = self.invoke("init", str(root))

            self.assertEqual(code, 4)
            self.assertEqual(stdout, "")
            self.assertIn("空でないディレクトリ", stderr)
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(root.iterdir()), [existing])

    def test_init_rejects_initialized_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "story-pipeline-config.jsonc"
            config.write_text("{}", encoding="utf-8")

            code, _, stderr = self.invoke("init", str(root))

            self.assertEqual(code, 4)
            self.assertIn(str(config), stderr)

    def test_init_rejects_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing"
            code, _, stderr = self.invoke("init", str(missing))
            self.assertEqual(code, 4)
            self.assertIn("存在しません", stderr)
            self.assertFalse(missing.exists())


class ScaffoldTest(unittest.TestCase):
    def test_creation_failure_rolls_back_created_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original_write_text = Path.write_text
            call_count = 0

            def failing_write_text(path: Path, *args: object, **kwargs: object) -> int:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("simulated failure")
                return original_write_text(path, *args, **kwargs)

            with mock.patch.object(Path, "write_text", failing_write_text):
                with self.assertRaises(OSError):
                    create_scaffold(root)

            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
