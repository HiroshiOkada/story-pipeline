from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.concept import (
    CONCEPT_HEADINGS,
    ConceptCandidate,
    EvaluatedConceptCandidate,
    build_concept_context,
    build_concept_revision_messages,
    check_concept_markdown,
    concept_evaluation_response_format,
    parse_concept_evaluation,
    run_concept_revision_loop,
    select_best_concept,
)
from story_pipeline.errors import StoryPipelineError
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

    def test_mechanical_check_normalizes_single_fence(self) -> None:
        body = "\n\n".join(f"{heading}\n内容" for heading in CONCEPT_HEADINGS)
        checked = check_concept_markdown(f"```markdown\n{body}\n```")
        self.assertTrue(checked.accepted)
        self.assertFalse(checked.content.startswith("```"))

    def test_mechanical_check_reports_structure_and_template_issues(self) -> None:
        body = "\n\n".join(
            f"{heading}\n{'<!-- 記入 -->' if index == 0 else '内容'}"
            for index, heading in enumerate(reversed(CONCEPT_HEADINGS))
        )
        checked = check_concept_markdown(body)
        self.assertFalse(checked.accepted)
        self.assertEqual(
            {issue.code for issue in checked.issues},
            {"TEMPLATE_COMMENT", "HEADING_ORDER"},
        )

    def test_mechanical_check_reports_missing_duplicate_and_empty_sections(self) -> None:
        headings = list(CONCEPT_HEADINGS)
        body = "\n\n".join(f"{heading}\n内容" for heading in headings[:-1])
        body = body.replace(f"{headings[1]}\n内容", f"{headings[1]}\n\n{headings[1]}\n内容")
        checked = check_concept_markdown(body)
        codes = {issue.code for issue in checked.issues}
        self.assertIn("MISSING_HEADING", codes)
        self.assertIn("DUPLICATE_HEADING", codes)

    def test_mechanical_check_rejects_explanation_and_internal_fence(self) -> None:
        body = "\n\n".join(f"{heading}\n内容" for heading in CONCEPT_HEADINGS)
        checked = check_concept_markdown("以下が構想です。\n" + body + "\n```\n補足\n```\n")
        self.assertEqual(
            {issue.code for issue in checked.issues},
            {"UNEXPECTED_PREAMBLE", "FENCE_REMAINS"},
        )

    def test_evaluation_requires_comparison_scores_and_derives_adoptability(self) -> None:
        value = {
            "decision": "accept",
            "summary": "条件を満たす",
            "issues": [],
            "scores": {"request_fit": 5, "consistency": 4, "appeal": 3},
        }
        evaluation = parse_concept_evaluation(json.dumps(value, ensure_ascii=False))
        self.assertTrue(evaluation.adoptable)
        self.assertEqual(evaluation.score("request_fit"), 5)
        value["scores"] = {"request_fit": 5}
        with self.assertRaises(StoryPipelineError):
            parse_concept_evaluation(json.dumps(value, ensure_ascii=False))

    def test_evaluation_error_blocks_adoption_even_if_reviewer_says_accept(self) -> None:
        value = {
            "decision": "accept",
            "summary": "矛盾あり",
            "issues": [
                {
                    "severity": "error",
                    "category": "required_condition",
                    "location": "## 禁止事項",
                    "evidence": "夢落ちを採用している",
                    "instruction": "夢落ちを除く",
                }
            ],
            "scores": {"request_fit": 2, "consistency": 3},
        }
        evaluation = parse_concept_evaluation(json.dumps(value, ensure_ascii=False))
        self.assertFalse(evaluation.adoptable)

    def test_evaluation_schema_is_strict(self) -> None:
        schema = concept_evaluation_response_format()["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]["scores"]["required"]),
            {"request_fit", "consistency"},
        )

    def test_revision_loop_stops_on_accept_and_preserves_bounded_context(self) -> None:
        context = build_concept_context(self.root, self.request, self.interpretation)
        initial_candidate = ConceptCandidate("初稿\n", 1, "writer", context.input_hashes)
        revise_value = {
            "decision": "revise",
            "summary": "改稿が必要",
            "issues": [],
            "scores": {"request_fit": 3, "consistency": 3},
        }
        accept_value = {
            "decision": "accept",
            "summary": "採用可能",
            "issues": [],
            "scores": {"request_fit": 5, "consistency": 5},
        }
        initial = EvaluatedConceptCandidate(
            initial_candidate,
            parse_concept_evaluation(json.dumps(revise_value)),
        )

        def revise(candidate, evaluation, revision_count):
            messages = build_concept_revision_messages(context, candidate, evaluation)
            self.assertIn("BEGIN CONCEPT CANDIDATE", messages[-3]["content"])
            return ConceptCandidate("改稿\n", 2, "reviser", context.input_hashes, revision_count)

        def review(_candidate):
            return parse_concept_evaluation(json.dumps(accept_value))

        records = run_concept_revision_loop(initial, 3, revise, review)
        self.assertEqual(len(records), 2)
        self.assertTrue(records[-1].evaluation.adoptable)

    def test_revision_loop_stops_for_human_and_obeys_limit(self) -> None:
        awaiting = parse_concept_evaluation(
            json.dumps(
                {
                    "decision": "awaiting_human",
                    "summary": "判断が必要",
                    "issues": [],
                    "scores": {"request_fit": 3, "consistency": 3},
                }
            )
        )
        candidate = ConceptCandidate("候補\n", 1, "writer", ())
        records = run_concept_revision_loop(
            EvaluatedConceptCandidate(candidate, awaiting),
            3,
            lambda *_: self.fail("改稿してはいけません"),
            lambda _: self.fail("評価してはいけません"),
        )
        self.assertEqual(len(records), 1)

    def test_best_candidate_uses_scores_then_fewer_revisions(self) -> None:
        evaluation = parse_concept_evaluation(
            json.dumps(
                {
                    "decision": "accept",
                    "summary": "採用可能",
                    "issues": [],
                    "scores": {"request_fit": 5, "consistency": 5},
                }
            )
        )
        first = EvaluatedConceptCandidate(ConceptCandidate("A", 1, "writer", ()), evaluation)
        revised = EvaluatedConceptCandidate(
            ConceptCandidate("B", 2, "reviser", (), 1), evaluation
        )
        self.assertIs(select_best_concept([revised, first]), first)


if __name__ == "__main__":
    unittest.main()
