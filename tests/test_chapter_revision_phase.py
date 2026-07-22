from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.chapter_revision import (
    DEFAULT_CHAPTER_REVISION_CONTEXT,
    build_chapter_revision_context,
)
from story_pipeline.errors import StoryPipelineError
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


class ChapterRevisionContextTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n第1章を仕上げてください。\n", encoding="utf-8"
        )
        for path in DEFAULT_CHAPTER_REVISION_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        for number in (1, 2):
            (self.root / "chapters" / f"{number:04d}.md").write_text(
                f"# 第{number}章\n\n## 収録話\n0001-0002\n\n## 完成後のあらすじ\n未作成\n",
                encoding="utf-8",
            )
        for number in (1, 2):
            (self.root / "episodes" / f"{number:04d}.md").write_text(
                f"## 話タイトル\n第{number}話\n\n## 本文\n本文{number}\n", encoding="utf-8"
            )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(json.dumps({
            "kind": "continue", "summary": "第1章を完成させる", "targets": [],
            "required_conditions": ["友情"], "prohibited_changes": ["夢落ち"],
            "additional_material": [], "decision_answers": [], "ambiguities": [],
            "requested_units": 1, "requested_until": None,
        }, ensure_ascii=False), self.request.content)

    def test_builds_context_with_all_episodes_and_adjacent_chapter(self) -> None:
        context = build_chapter_revision_context(
            self.root, self.request, self.interpretation, 1
        )
        self.assertEqual(context.chapter_path, "chapters/0001.md")
        self.assertEqual(context.episode_paths, ("episodes/0001.md", "episodes/0002.md"))
        self.assertIsNone(context.previous_chapter_path)
        self.assertEqual(context.next_chapter_path, "chapters/0002.md")
        for path in (*DEFAULT_CHAPTER_REVISION_CONTEXT, "chapters/0001.md", "episodes/0001.md", "episodes/0002.md", "chapters/0002.md"):
            digest = hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            self.assertIn(f"path={path} sha256={digest}", context.messages[3]["content"])

    def test_rejects_chapter_without_episode_range(self) -> None:
        (self.root / "chapters" / "0001.md").write_text("# 第1章\n", encoding="utf-8")
        with self.assertRaises(StoryPipelineError):
            build_chapter_revision_context(self.root, self.request, self.interpretation, 1)


if __name__ == "__main__":
    unittest.main()

