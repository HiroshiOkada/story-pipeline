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
    EvaluatedPlottingCandidate,
    PLOT_HEADINGS,
    PlottingCandidate,
    build_plotting_context,
    build_plotting_revision_messages,
    check_plotting_candidate,
    parse_plotting_candidate,
    parse_plotting_evaluation,
    plotting_evaluation_response_format,
    plotting_generation_response_format,
    run_plotting_revision_loop,
    select_best_plotting,
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

    def test_mechanical_check_accepts_complete_sequential_bundle(self) -> None:
        plot = "```markdown\n" + "\n\n".join(
            f"{heading}\n{'chapters/0001.md と chapters/0002.md' if heading == '## 章構成' else '内容'}"
            for heading in PLOT_HEADINGS
        ) + "\n```"
        chapters = []
        for number, episode_range in ((1, "0001〜0003"), (2, "0004〜0005")):
            content = "\n\n".join(
                f"{heading}\n{episode_range if heading == '## 収録話' else '内容'}"
                for heading in CHAPTER_HEADINGS
            )
            chapters.append((f"chapters/{number:04d}.md", content))
        candidate = parse_plotting_candidate(
            json.dumps(
                {
                    "plot.md": plot,
                    "chapters": [{"path": path, "content": content} for path, content in chapters],
                }
            ),
            generation=1,
            model_reference="mock",
            input_hashes=(),
        )
        checked = check_plotting_candidate(candidate)
        self.assertTrue(checked.accepted)
        self.assertFalse(checked.plot.startswith("```"))

    def test_mechanical_check_reports_structure_references_and_episode_gaps(self) -> None:
        plot = "\n\n".join(f"{heading}\n内容" for heading in PLOT_HEADINGS)
        chapter = "\n\n".join(
            f"{heading}\n{'0002〜0003' if heading == '## 収録話' else '内容'}"
            for heading in reversed(CHAPTER_HEADINGS)
        )
        candidate = parse_plotting_candidate(
            json.dumps(
                {
                    "plot.md": plot,
                    "chapters": [{"path": "chapters/0001.md", "content": chapter}],
                }
            ),
            generation=1,
            model_reference="mock",
            input_hashes=(),
        )
        codes = {issue.code for issue in check_plotting_candidate(candidate).issues}
        self.assertIn("CHAPTER_NOT_IN_PLOT", codes)
        self.assertIn("EPISODE_RANGE_SEQUENCE", codes)
        self.assertIn("HEADING_ORDER", codes)

    def test_evaluation_requires_plotting_scores_and_error_blocks_adoption(self) -> None:
        value = {
            "decision": "accept",
            "summary": "採用可能",
            "issues": [],
            "scores": {
                "request_fit": 5,
                "foundation_fit": 5,
                "causal_consistency": 4,
                "foreshadowing": 4,
            },
        }
        self.assertTrue(parse_plotting_evaluation(json.dumps(value)).adoptable)
        value["issues"] = [
            {
                "severity": "error",
                "category": "causality",
                "location": "plot.md ## 結末",
                "evidence": "転換点から結末へ接続しない",
                "instruction": "因果を追加する",
            }
        ]
        self.assertFalse(parse_plotting_evaluation(json.dumps(value)).adoptable)
        del value["scores"]["foreshadowing"]
        with self.assertRaises(StoryPipelineError):
            parse_plotting_evaluation(json.dumps(value))

    def test_evaluation_schema_requires_causality_and_foreshadowing(self) -> None:
        schema = plotting_evaluation_response_format()["json_schema"]["schema"]
        self.assertEqual(
            set(schema["properties"]["scores"]["required"]),
            {"request_fit", "foundation_fit", "causal_consistency", "foreshadowing"},
        )
        self.assertFalse(schema["additionalProperties"])

    def test_revision_loop_preserves_bundle_and_stops_on_accept(self) -> None:
        context = build_plotting_context(self.root, self.request, self.interpretation)
        scores = {
            "request_fit": 5,
            "foundation_fit": 5,
            "causal_consistency": 5,
            "foreshadowing": 5,
        }
        revise_evaluation = parse_plotting_evaluation(
            json.dumps({"decision": "revise", "summary": "改稿", "issues": [], "scores": scores})
        )
        accepted = parse_plotting_evaluation(
            json.dumps({"decision": "accept", "summary": "採用", "issues": [], "scores": scores})
        )
        initial = EvaluatedPlottingCandidate(
            PlottingCandidate("初稿", (("chapters/0001.md", "初稿"),), 1, "writer", context.input_hashes),
            revise_evaluation,
        )

        def revise(candidate, evaluation, revision_count):
            messages = build_plotting_revision_messages(context, candidate, evaluation)
            self.assertIn("BEGIN PLOTTING CANDIDATE", messages[-3]["content"])
            return PlottingCandidate(
                "改稿",
                (("chapters/0001.md", "改稿"),),
                2,
                "reviser",
                context.input_hashes,
                revision_count,
            )

        records = run_plotting_revision_loop(initial, 3, revise, lambda _: accepted)
        self.assertEqual(len(records), 2)
        self.assertIs(select_best_plotting(records), records[-1])

    def test_best_plotting_prefers_scores_then_fewer_revisions(self) -> None:
        evaluation = parse_plotting_evaluation(
            json.dumps(
                {
                    "decision": "accept",
                    "summary": "採用",
                    "issues": [],
                    "scores": {
                        "request_fit": 5,
                        "foundation_fit": 5,
                        "causal_consistency": 5,
                        "foreshadowing": 5,
                    },
                }
            )
        )
        chapters = (("chapters/0001.md", "内容"),)
        first = EvaluatedPlottingCandidate(PlottingCandidate("A", chapters, 1, "writer", ()), evaluation)
        revised = EvaluatedPlottingCandidate(PlottingCandidate("B", chapters, 2, "reviser", (), 1), evaluation)
        self.assertIs(select_best_plotting([revised, first]), first)


if __name__ == "__main__":
    unittest.main()
