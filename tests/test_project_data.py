from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.config import load_config
from story_pipeline.errors import StoryPipelineError
from story_pipeline.jsonc import load_jsonc
from story_pipeline.project import find_project_root
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


class ProjectRootTest(unittest.TestCase):
    def test_finds_nearest_root_from_descendant_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            nested = root / "notes" / "draft.md"
            nested.parent.mkdir()
            nested.write_text("", encoding="utf-8")
            self.assertEqual(find_project_root(nested), root.resolve())

    def test_missing_root_uses_not_initialized_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(StoryPipelineError) as raised:
                find_project_root(Path(temporary_directory))
            self.assertEqual(raised.exception.exit_code, 3)


class JsoncTest(unittest.TestCase):
    def test_allows_comments_and_trailing_commas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.jsonc"
            path.write_text(
                '{\n  // comment\n  "url": "https://example.test//path",\n'
                '  /* block\n     comment */\n  "items": [1, 2,],\n}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                load_jsonc(path),
                {"url": "https://example.test//path", "items": [1, 2]},
            )

    def test_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.jsonc"
            path.write_text('{"key": 1, "key": 2}', encoding="utf-8")
            with self.assertRaises(StoryPipelineError) as raised:
                load_jsonc(path)
            self.assertIn("重複", raised.exception.reason)

    def test_rejects_unclosed_block_comment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.jsonc"
            path.write_text('{"key": 1 /* open}', encoding="utf-8")
            with self.assertRaises(StoryPipelineError):
                load_jsonc(path)


class ConfigTest(unittest.TestCase):
    def test_scaffold_config_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            config = load_config(root)
            self.assertEqual(config["models"]["default"]["max_tokens"], 131072)
            self.assertEqual(
                config["models"]["default"]["parameters"],
                {"reasoning_effort": "none"},
            )
            self.assertEqual(config["dotenv"]["files"][1], str(root / ".env"))
            self.assertEqual(
                config["providers"]["openai"]["base_url"],
                "https://api.openai.com/v1",
            )

    def test_rejects_unknown_key_with_json_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            path = root / "story-pipeline-config.jsonc"
            config = json.loads(path.read_text(encoding="utf-8"))
            config["request"]["unknown"] = True
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(StoryPipelineError) as raised:
                load_config(root)
            self.assertEqual(raised.exception.location, "/request/unknown")

    def test_rejects_missing_model_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            path = root / "story-pipeline-config.jsonc"
            config = json.loads(path.read_text(encoding="utf-8"))
            config["roles"]["writer"] = ["missing"]
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(StoryPipelineError) as raised:
                load_config(root)
            self.assertEqual(raised.exception.location, "/roles/writer/0")

    def test_rejects_boolean_integer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            path = root / "story-pipeline-config.jsonc"
            config = json.loads(path.read_text(encoding="utf-8"))
            config["limits"]["generation_calls"] = True
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(StoryPipelineError):
                load_config(root)


class StateTest(unittest.TestCase):
    def test_scaffold_state_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            state = load_state(root)
            self.assertEqual(state["phase"], "concept")
            self.assertEqual(state["next_episode"], 1)

    def test_rejects_unsorted_completed_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            path = root / ".story-pipeline" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["completed_episodes"] = [2, 1]
            state["next_episode"] = 3
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(StoryPipelineError) as raised:
                load_state(root)
            self.assertEqual(raised.exception.location, "/completed_episodes")

    def test_rejects_comments_as_non_strict_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            path = root / ".story-pipeline" / "state.json"
            path.write_text("// comment\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaises(StoryPipelineError):
                load_state(root)

    def test_completed_phase_rejects_active_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            path = root / ".story-pipeline" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["phase"] = "completed"
            state["active_request"] = 0
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(StoryPipelineError) as raised:
                load_state(root)
            self.assertEqual(raised.exception.location, "/active_request")

    def test_9999_sentinel_is_only_allowed_in_terminal_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_scaffold(root)
            path = root / ".story-pipeline/state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state.update({
                "phase": "chapter_revision", "current_chapter": 9999,
                "next_chapter": 9999, "next_episode": 9999,
                "completed_episodes": [9999],
            })
            path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(load_state(root)["next_episode"], 9999)

            state["phase"] = "episode_planning"
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(StoryPipelineError):
                load_state(root)


if __name__ == "__main__":
    unittest.main()
