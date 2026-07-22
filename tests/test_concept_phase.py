from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.concept import CONCEPT_HEADINGS, build_concept_context
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def interpretation_json(**updates: object) -> str:
    value: dict[str, object] = {
        "kind": "create",
        "summary": "短編の構想を作る",
        "targets": [],
        "required_conditions": ["海辺を舞台にする"],
        "prohibited_changes": ["夢落ちにしない"],
        "additional_material": [],
        "decision_answers": [],
        "ambiguities": [],
        "requested_units": 1,
        "requested_until": None,
    }
    value.update(updates)
    return json.dumps(value, ensure_ascii=False)


class ConceptPhaseTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        self.request_path = self.root / "requests" / "0000.md"
        self.request_path.write_text("# 作品作成要求\n\n海辺の短編を書いてください。\n", encoding="utf-8")
        self.state = load_state(self.root)
        self.request = select_request(self.root, self.state)
        self.interpretation = parse_request_interpretation(
            interpretation_json(), self.request.content
        )

    def test_builds_bounded_concept_context_in_priority_order(self) -> None:
        context = build_concept_context(self.root, self.request, self.interpretation)
        self.assertEqual([item["role"] for item in context.messages], ["system"] + ["user"] * 4)
        request_hash = hashlib.sha256(self.request_path.read_bytes()).hexdigest()
        self.assertIn(f"path=requests/0000.md sha256={request_hash}", context.messages[1]["content"])
        self.assertIn("海辺を舞台にする", context.messages[2]["content"])
        self.assertIn("なし", context.messages[3]["content"])
        self.assertEqual(context.input_hashes[0], ("requests/0000.md", request_hash))

    def test_generation_prompt_requires_all_concept_sections(self) -> None:
        context = build_concept_context(self.root, self.request, self.interpretation)
        for heading in CONCEPT_HEADINGS:
            self.assertIn(heading, context.messages[0]["content"])
        self.assertIn("指定順で一度ずつ", context.messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
