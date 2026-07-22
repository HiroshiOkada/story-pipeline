from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.errors import StoryPipelineError
from story_pipeline.plotting import (
    CHAPTER_HEADINGS,
    DEFAULT_PLOTTING_CONTEXT,
    PLOT_HEADINGS,
    build_plotting_context,
    parse_plotting_candidate,
    plotting_generation_response_format,
)
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


class PlottingPhaseTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n全体構成を作ってください。\n", encoding="utf-8"
        )
        for path in DEFAULT_PLOTTING_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "continue",
                    "summary": "全体構成を作る",
                    "targets": [],
                    "required_conditions": ["友情の再生を結末まで描く"],
                    "prohibited_changes": ["超常現象を出さない"],
                    "additional_material": [],
                    "decision_answers": [],
                    "ambiguities": [],
                    "requested_units": 1,
                    "requested_until": None,
                },
                ensure_ascii=False,
            ),
            self.request.content,
        )

    def test_builds_context_with_all_adopted_foundation_documents(self) -> None:
        context = build_plotting_context(self.root, self.request, self.interpretation)
        self.assertEqual([item["role"] for item in context.messages], ["system"] + ["user"] * 4)
        for path in DEFAULT_PLOTTING_CONTEXT:
            digest = hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            self.assertIn(f"path={path} sha256={digest}", context.messages[3]["content"])
        self.assertIn("友情の再生", context.messages[2]["content"])

    def test_candidate_contract_keeps_plot_and_ordered_chapters_together(self) -> None:
        payload = {
            "plot.md": "# 全体構成\n",
            "chapters": [
                {"path": "chapters/0001.md", "content": "# 第一章\n"},
                {"path": "chapters/0002.md", "content": "# 第二章\n"},
            ],
        }
        candidate = parse_plotting_candidate(
            json.dumps(payload), generation=1, model_reference="mock", input_hashes=()
        )
        self.assertEqual(tuple(path for path, _ in candidate.documents), (
            "plot.md", "chapters/0001.md", "chapters/0002.md"
        ))
        payload["chapters"][1]["path"] = "chapters/0003.md"
        with self.assertRaises(StoryPipelineError):
            parse_plotting_candidate(
                json.dumps(payload), generation=1, model_reference="mock", input_hashes=()
            )

    def test_generation_prompt_and_schema_require_plot_and_chapters(self) -> None:
        context = build_plotting_context(self.root, self.request, self.interpretation)
        for heading in (*PLOT_HEADINGS, *CHAPTER_HEADINGS):
            self.assertIn(heading, context.messages[0]["content"])
        schema = plotting_generation_response_format()["json_schema"]["schema"]
        self.assertEqual(set(schema["required"]), {"plot.md", "chapters"})
        self.assertEqual(schema["properties"]["chapters"]["minItems"], 1)


if __name__ == "__main__":
    unittest.main()
