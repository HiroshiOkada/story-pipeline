from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.episode_planning import (
    DEFAULT_EPISODE_PLANNING_CONTEXT,
    EPISODE_PLAN_HEADINGS,
    build_episode_planning_context,
    episode_plan_generation_response_format,
    parse_episode_plan_candidate,
)
from story_pipeline.errors import StoryPipelineError
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


class EpisodePlanningPhaseTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n次の話を計画してください。\n", encoding="utf-8"
        )
        for path in DEFAULT_EPISODE_PLANNING_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        (self.root / "chapters" / "0001.md").write_text(
            "# 第一章\n\n## 目的\n再会\n\n## 収録話\n0001〜0003\n", encoding="utf-8"
        )
        (self.root / "episodes" / "0001.md").write_text(
            "# 第1話\n\n## 本文\n二人は再会した。\n", encoding="utf-8"
        )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "continue", "summary": "次話を計画する", "targets": [],
                    "required_conditions": ["友情を描く"], "prohibited_changes": ["夢落ち"],
                    "additional_material": [], "decision_answers": [], "ambiguities": [],
                    "requested_units": 1, "requested_until": None,
                },
                ensure_ascii=False,
            ),
            self.request.content,
        )

    def test_builds_context_with_target_chapter_and_previous_episode(self) -> None:
        context = build_episode_planning_context(
            self.root, self.request, self.interpretation, 2
        )
        self.assertEqual(context.chapter_path, "chapters/0001.md")
        self.assertEqual(context.previous_episode_path, "episodes/0001.md")
        self.assertIn("二人は再会した", context.messages[3]["content"])
        for path in (*DEFAULT_EPISODE_PLANNING_CONTEXT, "chapters/0001.md", "episodes/0001.md"):
            digest = hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            self.assertIn(f"path={path} sha256={digest}", context.messages[3]["content"])

    def test_candidate_contract_rejects_path_for_another_episode(self) -> None:
        candidate = parse_episode_plan_candidate(
            json.dumps({"path": "episode_plans/0002.md", "content": "# 第2話"}),
            episode_number=2, generation=1, model_reference="mock", input_hashes=(),
        )
        self.assertEqual(candidate.path, "episode_plans/0002.md")
        with self.assertRaises(StoryPipelineError):
            parse_episode_plan_candidate(
                json.dumps({"path": "episode_plans/0003.md", "content": "# 第3話"}),
                episode_number=2, generation=1, model_reference="mock", input_hashes=(),
            )

    def test_generation_prompt_and_schema_pin_headings_and_target_path(self) -> None:
        context = build_episode_planning_context(
            self.root, self.request, self.interpretation, 2
        )
        for heading in EPISODE_PLAN_HEADINGS:
            self.assertIn(heading, context.messages[0]["content"])
        schema = episode_plan_generation_response_format(2)["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["path"]["const"], "episode_plans/0002.md")
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
