from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from story_pipeline.errors import StoryPipelineError
from story_pipeline.story_structure import load_story_structure, parse_chapter_episodes


class StoryStructureTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "chapters").mkdir()

    def write_chapter(self, number: int, episodes: str) -> None:
        (self.root / f"chapters/{number:04d}.md").write_text(
            f"# 第{number}章\n\n## 収録話\n{episodes}\n\n## 接続条件\n次へ進む。\n",
            encoding="utf-8",
        )

    def test_expands_ranges_and_builds_continuous_mapping(self) -> None:
        self.write_chapter(1, "0001〜0002")
        self.write_chapter(2, "0003-0004")

        structure = load_story_structure(self.root)

        self.assertEqual(structure.chapter_numbers, (1, 2))
        self.assertEqual(structure.episode_numbers, (1, 2, 3, 4))
        self.assertEqual(structure.chapter_for_episode(3).number, 2)

    def test_rejects_reverse_duplicate_and_gap_with_section_location(self) -> None:
        for label, value in (("reverse", "0002 0001"), ("duplicate", "0001 0001"), ("gap", "0001 0003")):
            with self.subTest(label=label):
                with self.assertRaises(StoryPipelineError) as raised:
                    parse_chapter_episodes(f"## 収録話\n{value}\n", "chapters/0001.md")
                self.assertEqual(raised.exception.location, "chapters/0001.md ## 収録話")

    def test_rejects_cross_chapter_overlap_gap_and_missing_chapter(self) -> None:
        cases = ((1, "0001", 2, "0001"), (1, "0001", 2, "0003"), (1, "0001", 3, "0002"))
        for first_number, first, second_number, second in cases:
            with self.subTest(case=(first_number, first, second_number, second)):
                for path in (self.root / "chapters").glob("*.md"):
                    path.unlink()
                self.write_chapter(first_number, first)
                self.write_chapter(second_number, second)
                with self.assertRaises(StoryPipelineError):
                    load_story_structure(self.root)
