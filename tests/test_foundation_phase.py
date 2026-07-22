from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.foundation import (
    FOUNDATION_FILES,
    FOUNDATION_HEADINGS,
    build_foundation_context,
    foundation_generation_response_format,
    parse_foundation_candidate,
)
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


class FoundationPhaseTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        request_path = self.root / "requests" / "0000.md"
        request_path.write_text("# 作品作成要求\n\n基礎設定を作ってください。\n", encoding="utf-8")
        self.concept_path = self.root / "concept.md"
        self.concept_path.write_text("# 構想\n\n海辺の青春短編。\n", encoding="utf-8")
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "continue",
                    "summary": "基礎設定を作る",
                    "targets": [],
                    "required_conditions": ["現代日本の海辺"],
                    "prohibited_changes": ["超能力を出さない"],
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

    def test_builds_context_with_adopted_concept_and_priority(self) -> None:
        context = build_foundation_context(self.root, self.request, self.interpretation)
        self.assertEqual([item["role"] for item in context.messages], ["system"] + ["user"] * 4)
        concept_hash = hashlib.sha256(self.concept_path.read_bytes()).hexdigest()
        self.assertIn(f"path=concept.md sha256={concept_hash}", context.messages[3]["content"])
        self.assertIn("現代日本の海辺", context.messages[2]["content"])
        self.assertEqual(context.input_hashes[-1], ("concept.md", concept_hash))
        for path, headings in FOUNDATION_HEADINGS.items():
            self.assertIn(path, context.messages[0]["content"])
            for heading in headings:
                self.assertIn(heading, context.messages[0]["content"])

    def test_candidate_contract_keeps_four_documents_together(self) -> None:
        payload = {path: f"# {path}\n" for path in FOUNDATION_FILES}
        candidate = parse_foundation_candidate(
            json.dumps(payload),
            generation=1,
            model_reference="mock-writer",
            input_hashes=(("concept.md", "abc"),),
        )
        self.assertEqual(tuple(name for name, _ in candidate.documents), FOUNDATION_FILES)
        self.assertEqual(candidate.content("style.md"), "# style.md\n")
        del payload["canon.md"]
        with self.assertRaises(Exception):
            parse_foundation_candidate(
                json.dumps(payload), generation=1, model_reference="mock", input_hashes=()
            )

    def test_generation_schema_requires_exactly_four_documents(self) -> None:
        schema = foundation_generation_response_format()["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(tuple(schema["required"]), FOUNDATION_FILES)


if __name__ == "__main__":
    unittest.main()
