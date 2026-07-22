from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.episode_planning import (
    DEFAULT_EPISODE_PLANNING_CONTEXT,
    EPISODE_PLAN_HEADINGS,
    EpisodePlanCandidate,
    EvaluatedEpisodePlanCandidate,
    build_episode_planning_context,
    build_episode_plan_revision_messages,
    check_episode_plan_candidate,
    episode_plan_generation_response_format,
    episode_plan_evaluation_response_format,
    parse_episode_plan_candidate,
    parse_episode_plan_evaluation,
    run_episode_plan_revision_loop,
    select_best_episode_plan,
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

    def test_mechanical_check_accepts_complete_plan_and_normalizes_fence(self) -> None:
        content = "```markdown\n# 第2話\n\n" + "\n\n".join(
            f"{heading}\n{'8,000字' if heading == '## 目標文字数' else '内容'}"
            for heading in EPISODE_PLAN_HEADINGS
        ) + "\n```"
        candidate = parse_episode_plan_candidate(
            json.dumps({"path": "episode_plans/0002.md", "content": content}),
            episode_number=2, generation=1, model_reference="mock", input_hashes=(),
        )
        checked = check_episode_plan_candidate(candidate)
        self.assertTrue(checked.accepted)
        self.assertEqual(checked.target_length, 8000)
        self.assertFalse(checked.content.startswith("```"))

    def test_mechanical_check_reports_order_empty_section_and_invalid_length(self) -> None:
        content = "\n\n".join(
            f"{heading}\n{'0字' if heading == '## 目標文字数' else ('' if heading == '## 場面' else '内容')}"
            for heading in reversed(EPISODE_PLAN_HEADINGS)
        )
        candidate = parse_episode_plan_candidate(
            json.dumps({"path": "episode_plans/0002.md", "content": content}),
            episode_number=2, generation=1, model_reference="mock", input_hashes=(),
        )
        codes = {issue.code for issue in check_episode_plan_candidate(candidate).issues}
        self.assertIn("HEADING_ORDER", codes)
        self.assertIn("INVALID_TARGET_LENGTH", codes)

    def test_evaluation_requires_episode_plan_scores_and_error_blocks_adoption(self) -> None:
        value = {
            "decision": "accept", "summary": "採用可能", "issues": [],
            "scores": {
                "request_fit": 5, "chapter_fit": 5, "continuity": 5,
                "causal_consistency": 4, "plan_completeness": 4, "length_fit": 5,
            },
        }
        self.assertTrue(parse_episode_plan_evaluation(json.dumps(value)).adoptable)
        value["issues"] = [{
            "severity": "error", "category": "causality", "location": "## 終了状態",
            "evidence": "場面から終了状態へ接続しない", "instruction": "原因となる行動を追加する",
        }]
        self.assertFalse(parse_episode_plan_evaluation(json.dumps(value)).adoptable)
        del value["scores"]["length_fit"]
        with self.assertRaises(StoryPipelineError):
            parse_episode_plan_evaluation(json.dumps(value))

    def test_evaluation_schema_requires_continuity_completeness_and_length(self) -> None:
        schema = episode_plan_evaluation_response_format()["json_schema"]["schema"]
        self.assertEqual(
            set(schema["properties"]["scores"]["required"]),
            {"request_fit", "chapter_fit", "continuity", "causal_consistency", "plan_completeness", "length_fit"},
        )
        self.assertFalse(schema["additionalProperties"])

    def test_revision_loop_preserves_target_and_stops_on_accept(self) -> None:
        context = build_episode_planning_context(
            self.root, self.request, self.interpretation, 2
        )
        scores = {
            "request_fit": 5, "chapter_fit": 5, "continuity": 5,
            "causal_consistency": 5, "plan_completeness": 5, "length_fit": 5,
        }
        revise_evaluation = parse_episode_plan_evaluation(json.dumps(
            {"decision": "revise", "summary": "改稿", "issues": [], "scores": scores}
        ))
        accepted = parse_episode_plan_evaluation(json.dumps(
            {"decision": "accept", "summary": "採用", "issues": [], "scores": scores}
        ))
        initial = EvaluatedEpisodePlanCandidate(
            EpisodePlanCandidate("episode_plans/0002.md", "初稿", 2, 1, "planner", context.input_hashes),
            revise_evaluation,
        )

        def revise(candidate, evaluation, revision_count):
            messages = build_episode_plan_revision_messages(context, candidate, evaluation)
            self.assertIn("BEGIN EPISODE PLAN CANDIDATE", messages[-3]["content"])
            return EpisodePlanCandidate(
                candidate.path, "改稿", 2, 2, "reviser", context.input_hashes, revision_count
            )

        records = run_episode_plan_revision_loop(initial, 3, revise, lambda _: accepted)
        self.assertEqual(len(records), 2)
        self.assertIs(select_best_episode_plan(records), records[-1])

    def test_best_episode_plan_prefers_scores_then_fewer_revisions(self) -> None:
        scores = {
            "request_fit": 5, "chapter_fit": 5, "continuity": 5,
            "causal_consistency": 5, "plan_completeness": 5, "length_fit": 5,
        }
        evaluation = parse_episode_plan_evaluation(json.dumps(
            {"decision": "accept", "summary": "採用", "issues": [], "scores": scores}
        ))
        first = EvaluatedEpisodePlanCandidate(
            EpisodePlanCandidate("episode_plans/0002.md", "A", 2, 1, "planner", ()), evaluation
        )
        revised = EvaluatedEpisodePlanCandidate(
            EpisodePlanCandidate("episode_plans/0002.md", "B", 2, 2, "reviser", (), 1), evaluation
        )
        self.assertIs(select_best_episode_plan([revised, first]), first)


if __name__ == "__main__":
    unittest.main()
