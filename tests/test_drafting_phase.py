from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.drafting import (
    DEFAULT_DRAFTING_CONTEXT,
    DraftCandidate,
    EPISODE_HEADINGS,
    EvaluatedDraftCandidate,
    build_draft_revision_messages,
    build_draft_knowledge_messages,
    build_drafting_context,
    check_draft_candidate,
    draft_generation_response_format,
    draft_evaluation_response_format,
    draft_knowledge_response_format,
    parse_draft_candidate,
    parse_draft_evaluation,
    parse_draft_knowledge_update,
    run_draft_revision_loop,
    select_best_draft,
)
from story_pipeline.errors import StoryPipelineError
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


class DraftingPhaseTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_scaffold(self.root)
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n次の話を執筆してください。\n", encoding="utf-8"
        )
        for path in DEFAULT_DRAFTING_CONTEXT:
            (self.root / path).write_text(f"# {path}\n\n採用済み内容\n", encoding="utf-8")
        for number in (1, 2, 3):
            (self.root / "episode_plans" / f"{number:04d}.md").write_text(
                f"# 第{number}話計画\n\n## 目標文字数\n100字\n", encoding="utf-8"
            )
        (self.root / "episodes" / "0001.md").write_text(
            "## 話タイトル\n再会\n\n## 本文\n二人は再会した。\n", encoding="utf-8"
        )
        self.request = select_request(self.root, load_state(self.root))
        self.interpretation = parse_request_interpretation(
            json.dumps({
                "kind": "continue", "summary": "第2話を執筆する", "targets": [],
                "required_conditions": ["友情"], "prohibited_changes": ["夢落ち"],
                "additional_material": [], "decision_answers": [], "ambiguities": [],
                "requested_units": 1, "requested_until": None,
            }, ensure_ascii=False),
            self.request.content,
        )

    def test_builds_context_with_plan_previous_episode_and_next_plan(self) -> None:
        context = build_drafting_context(self.root, self.request, self.interpretation, 2)
        self.assertEqual(context.plan_path, "episode_plans/0002.md")
        self.assertEqual(context.previous_episode_path, "episodes/0001.md")
        self.assertEqual(context.next_plan_path, "episode_plans/0003.md")
        self.assertEqual(context.target_length, 100)
        self.assertEqual(context.length_tolerance, 0.20)
        for path in (*DEFAULT_DRAFTING_CONTEXT, "episode_plans/0002.md", "episodes/0001.md", "episode_plans/0003.md"):
            digest = hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            self.assertIn(f"path={path} sha256={digest}", context.messages[3]["content"])

    def test_candidate_contract_rejects_path_for_another_episode(self) -> None:
        candidate = parse_draft_candidate(
            json.dumps({"path": "episodes/0002.md", "content": "本文"}),
            episode_number=2, generation=1, model_reference="mock", input_hashes=(),
        )
        self.assertEqual(candidate.path, "episodes/0002.md")
        with self.assertRaises(StoryPipelineError):
            parse_draft_candidate(
                json.dumps({"path": "episodes/0003.md", "content": "本文"}),
                episode_number=2, generation=1, model_reference="mock", input_hashes=(),
            )

    def test_generation_prompt_and_schema_pin_headings_target_and_length(self) -> None:
        context = build_drafting_context(self.root, self.request, self.interpretation, 2)
        for heading in EPISODE_HEADINGS:
            self.assertIn(heading, context.messages[0]["content"])
        self.assertIn("100字", context.messages[-1]["content"])
        schema = draft_generation_response_format(2)["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["path"]["const"], "episodes/0002.md")
        self.assertFalse(schema["additionalProperties"])

    def test_request_length_tolerance_overrides_style_and_default(self) -> None:
        (self.root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n第2話を100字±10%で執筆してください。\n", encoding="utf-8"
        )
        (self.root / "style.md").write_text(
            "# style\n\n文字数は±15%を許容する。\n", encoding="utf-8"
        )
        request = select_request(self.root, load_state(self.root))
        context = build_drafting_context(self.root, request, self.interpretation, 2)
        self.assertEqual(context.length_tolerance, 0.10)

    def test_mechanical_check_accepts_normalized_body_within_length(self) -> None:
        content = "```markdown\n## 話タイトル\n潮風\n\n## 本文\n" + "海" * 100 + "\n```"
        candidate = parse_draft_candidate(
            json.dumps({"path": "episodes/0002.md", "content": content}),
            episode_number=2, generation=1, model_reference="mock", input_hashes=(),
        )
        checked = check_draft_candidate(candidate, 100)
        self.assertTrue(checked.accepted)
        self.assertEqual(checked.character_count, 100)
        self.assertEqual(checked.issues, ())
        self.assertFalse(checked.content.startswith("```"))

    def test_mechanical_check_reports_structure_json_and_length_warning(self) -> None:
        content = "説明\n\n## 本文\n{}\n\n## 話タイトル\n題\n\n## 説明\n不要"
        candidate = parse_draft_candidate(
            json.dumps({"path": "episodes/0002.md", "content": content}),
            episode_number=2, generation=1, model_reference="mock", input_hashes=(),
        )
        checked = check_draft_candidate(candidate, 100)
        codes = {issue.code for issue in checked.issues}
        self.assertFalse(checked.accepted)
        self.assertIn("HEADING_ORDER", codes)
        self.assertIn("UNKNOWN_HEADING", codes)
        self.assertIn("LENGTH_OUT_OF_RANGE", codes)

    def test_evaluation_requires_draft_scores_and_error_blocks_adoption(self) -> None:
        value = {
            "decision": "accept", "summary": "採用可能", "issues": [],
            "scores": {
                "request_fit": 5, "consistency": 5, "plan_fit": 5,
                "episode_completion": 5, "style_fit": 4, "readability": 4,
            },
        }
        self.assertTrue(parse_draft_evaluation(json.dumps(value)).adoptable)
        value["issues"] = [{
            "severity": "error", "category": "continuity", "location": "## 本文",
            "evidence": "直前話の終了状態と矛盾する", "instruction": "開始状態を合わせる",
        }]
        self.assertFalse(parse_draft_evaluation(json.dumps(value)).adoptable)
        del value["scores"]["consistency"]
        with self.assertRaises(StoryPipelineError):
            parse_draft_evaluation(json.dumps(value))

    def test_evaluation_schema_requires_consistency_plan_and_completion(self) -> None:
        schema = draft_evaluation_response_format()["json_schema"]["schema"]
        self.assertEqual(
            set(schema["properties"]["scores"]["required"]),
            {"request_fit", "consistency", "plan_fit", "episode_completion", "style_fit", "readability"},
        )
        self.assertFalse(schema["additionalProperties"])

    def test_revision_loop_preserves_target_and_stops_on_accept(self) -> None:
        context = build_drafting_context(self.root, self.request, self.interpretation, 2)
        scores = {
            "request_fit": 5, "consistency": 5, "plan_fit": 5,
            "episode_completion": 5, "style_fit": 5, "readability": 5,
        }
        revise_evaluation = parse_draft_evaluation(json.dumps(
            {"decision": "revise", "summary": "改稿", "issues": [], "scores": scores}
        ))
        accepted = parse_draft_evaluation(json.dumps(
            {"decision": "accept", "summary": "採用", "issues": [], "scores": scores}
        ))
        initial = EvaluatedDraftCandidate(
            DraftCandidate("episodes/0002.md", "初稿", 2, 1, "writer", context.input_hashes),
            revise_evaluation,
        )

        def revise(candidate, evaluation, revision_count):
            messages = build_draft_revision_messages(context, candidate, evaluation)
            self.assertIn("BEGIN DRAFT CANDIDATE", messages[-3]["content"])
            return DraftCandidate(
                candidate.path, "改稿", 2, 2, "reviser", context.input_hashes, revision_count
            )

        records = run_draft_revision_loop(initial, 3, revise, lambda _: accepted)
        self.assertEqual(len(records), 2)
        self.assertIs(select_best_draft(records), records[-1])

    def test_best_draft_prefers_required_scores_then_fewer_revisions(self) -> None:
        scores = {
            "request_fit": 5, "consistency": 5, "plan_fit": 5,
            "episode_completion": 5, "style_fit": 5, "readability": 5,
        }
        evaluation = parse_draft_evaluation(json.dumps(
            {"decision": "accept", "summary": "採用", "issues": [], "scores": scores}
        ))
        first = EvaluatedDraftCandidate(
            DraftCandidate("episodes/0002.md", "A", 2, 1, "writer", ()), evaluation
        )
        revised = EvaluatedDraftCandidate(
            DraftCandidate("episodes/0002.md", "B", 2, 2, "reviser", (), 1), evaluation
        )
        self.assertIs(select_best_draft([revised, first]), first)

    def test_knowledge_update_requires_unique_evidence_from_accepted_draft(self) -> None:
        candidate = DraftCandidate(
            "episodes/0002.md",
            "## 話タイトル\n潮風\n\n## 本文\n凪は湊と看板を直し、握手した。\n",
            2, 1, "writer", (),
        )
        payload = {
            "canon_facts": [{
                "fact": "看板の修理が完了した", "evidence": "凪は湊と看板を直し、握手した。",
                "source": "episodes/0002.md", "established_at": "第0002話終了時", "people": ["凪", "湊"],
            }],
            "character_states": [{
                "character": "凪", "state": "湊との友情を回復した",
                "evidence": "凪は湊と看板を直し、握手した。", "source": "episodes/0002.md",
                "established_at": "第0002話終了時",
            }],
        }
        update = parse_draft_knowledge_update(json.dumps(payload, ensure_ascii=False), candidate)
        self.assertEqual(update.canon_facts[0].people, ("凪", "湊"))
        self.assertEqual(update.character_states[0].character, "凪")
        payload["canon_facts"][0]["evidence"] = "本文にない事実"
        with self.assertRaises(StoryPipelineError):
            parse_draft_knowledge_update(json.dumps(payload, ensure_ascii=False), candidate)

    def test_knowledge_evidence_resolves_whitespace_to_original_text(self) -> None:
        candidate = DraftCandidate(
            "episodes/0001.md",
            "## 話タイトル\n潮風\n\n## 本文\n凪は古い看板を\n二人で直した。凪は笑った。\n",
            1, 1, "writer", (),
        )
        payload = {
            "canon_facts": [{
                "fact": "二人で看板を直した",
                "evidence": "凪は 古い看板を二人で 直した。",
                "source": "episodes/0001.md",
                "established_at": "第0001話",
                "people": ["凪"],
            }],
            "character_states": [],
        }

        update = parse_draft_knowledge_update(json.dumps(payload, ensure_ascii=False), candidate)

        self.assertEqual(update.canon_facts[0].evidence, "凪は古い看板を\n二人で直した。")
        payload["canon_facts"][0]["evidence"] = "凪"
        with self.assertRaises(StoryPipelineError):
            parse_draft_knowledge_update(json.dumps(payload, ensure_ascii=False), candidate)

    def test_knowledge_prompt_and_schema_pin_source_to_accepted_episode(self) -> None:
        context = build_drafting_context(self.root, self.request, self.interpretation, 2)
        candidate = DraftCandidate(
            "episodes/0002.md", "## 話タイトル\n題\n\n## 本文\n本文\n", 2, 1, "writer", (),
        )
        messages = build_draft_knowledge_messages(context, candidate)
        self.assertIn("BEGIN ACCEPTED DRAFT", messages[-2]["content"])
        schema = draft_knowledge_response_format(2)["json_schema"]["schema"]
        source = schema["properties"]["canon_facts"]["items"]["properties"]["source"]
        self.assertEqual(source["const"], "episodes/0002.md")


if __name__ == "__main__":
    unittest.main()
