from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.foundation import (
    FOUNDATION_FILES,
    FOUNDATION_HEADINGS,
    EvaluatedFoundationCandidate,
    FoundationCandidate,
    build_foundation_context,
    build_foundation_revision_messages,
    check_foundation_documents,
    foundation_evaluation_response_format,
    foundation_generation_response_format,
    parse_foundation_evaluation,
    parse_foundation_candidate,
    run_foundation_revision_loop,
    select_best_foundation,
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

    def test_mechanical_check_normalizes_and_accepts_complete_documents(self) -> None:
        documents = {
            path: "```markdown\n"
            + "\n\n".join(f"{heading}\n内容" for heading in headings)
            + "\n```"
            for path, headings in FOUNDATION_HEADINGS.items()
        }
        checked = check_foundation_documents(documents)
        self.assertTrue(checked.accepted)
        self.assertFalse(checked.content("world.md").startswith("```"))

    def test_mechanical_check_reports_structure_across_documents(self) -> None:
        documents = {
            path: "\n\n".join(f"{heading}\n内容" for heading in headings)
            for path, headings in FOUNDATION_HEADINGS.items()
        }
        documents["world.md"] = "説明\n" + documents["world.md"].replace(
            FOUNDATION_HEADINGS["world.md"][-1], "## 別の節"
        )
        documents["style.md"] += "\n<!-- TODO -->\n````\n"
        checked = check_foundation_documents(documents)
        codes = {issue.code for issue in checked.issues}
        self.assertIn("MISSING_HEADING", codes)
        self.assertIn("UNEXPECTED_PREAMBLE", codes)
        self.assertIn("TEMPLATE_COMMENT", codes)
        self.assertIn("FENCE_REMAINS", codes)

    def test_mechanical_check_rejects_provisional_future_plan_in_canon(self) -> None:
        documents = {
            path: "\n\n".join(f"{heading}\n内容" for heading in headings)
            for path, headings in FOUNDATION_HEADINGS.items()
        }
        documents["canon.md"] += "\n- 将来案: 主人公は町を去る\n"
        checked = check_foundation_documents(documents)
        self.assertIn("PROVISIONAL_CANON", {issue.code for issue in checked.issues})

    def test_evaluation_requires_scores_and_error_blocks_adoption(self) -> None:
        value = {
            "decision": "accept",
            "summary": "整合している",
            "issues": [],
            "scores": {"request_fit": 5, "concept_fit": 5, "consistency": 4},
        }
        evaluation = parse_foundation_evaluation(json.dumps(value, ensure_ascii=False))
        self.assertTrue(evaluation.adoptable)
        value["issues"] = [
            {
                "severity": "error",
                "category": "cross_document_consistency",
                "location": "world.md / characters.md",
                "evidence": "能力が世界ルールに反する",
                "instruction": "能力を世界ルールへ合わせる",
            }
        ]
        self.assertFalse(
            parse_foundation_evaluation(json.dumps(value, ensure_ascii=False)).adoptable
        )
        value["scores"] = {"request_fit": 5, "consistency": 4}
        with self.assertRaises(Exception):
            parse_foundation_evaluation(json.dumps(value, ensure_ascii=False))

    def test_evaluation_schema_requires_foundation_scores(self) -> None:
        schema = foundation_evaluation_response_format()["json_schema"]["schema"]
        self.assertEqual(
            set(schema["properties"]["scores"]["required"]),
            {"request_fit", "concept_fit", "consistency"},
        )
        self.assertFalse(schema["additionalProperties"])

    def test_revision_loop_preserves_four_files_and_stops_on_accept(self) -> None:
        context = build_foundation_context(self.root, self.request, self.interpretation)
        documents = tuple((path, f"{path} 初稿") for path in FOUNDATION_FILES)
        revise_evaluation = parse_foundation_evaluation(
            json.dumps(
                {
                    "decision": "revise",
                    "summary": "整合性を直す",
                    "issues": [],
                    "scores": {"request_fit": 4, "concept_fit": 4, "consistency": 2},
                }
            )
        )
        accepted = parse_foundation_evaluation(
            json.dumps(
                {
                    "decision": "accept",
                    "summary": "採用可能",
                    "issues": [],
                    "scores": {"request_fit": 5, "concept_fit": 5, "consistency": 5},
                }
            )
        )
        initial = EvaluatedFoundationCandidate(
            FoundationCandidate(documents, 1, "writer", context.input_hashes),
            revise_evaluation,
        )

        def revise(candidate, evaluation, revision_count):
            messages = build_foundation_revision_messages(context, candidate, evaluation)
            self.assertIn("BEGIN FOUNDATION CANDIDATE", messages[-3]["content"])
            return FoundationCandidate(
                tuple((path, f"{path} 改稿") for path in FOUNDATION_FILES),
                2,
                "reviser",
                context.input_hashes,
                revision_count,
            )

        records = run_foundation_revision_loop(initial, 3, revise, lambda _: accepted)
        self.assertEqual(len(records), 2)
        self.assertEqual(tuple(name for name, _ in records[-1].candidate.documents), FOUNDATION_FILES)
        self.assertIs(select_best_foundation(records), records[-1])

    def test_best_foundation_prefers_scores_then_fewer_revisions(self) -> None:
        evaluation = parse_foundation_evaluation(
            json.dumps(
                {
                    "decision": "accept",
                    "summary": "採用可能",
                    "issues": [],
                    "scores": {"request_fit": 5, "concept_fit": 5, "consistency": 5},
                }
            )
        )
        documents = tuple((path, "内容") for path in FOUNDATION_FILES)
        first = EvaluatedFoundationCandidate(
            FoundationCandidate(documents, 1, "writer", ()), evaluation
        )
        revised = EvaluatedFoundationCandidate(
            FoundationCandidate(documents, 2, "reviser", (), 1), evaluation
        )
        self.assertIs(select_best_foundation([revised, first]), first)


if __name__ == "__main__":
    unittest.main()
