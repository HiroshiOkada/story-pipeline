from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from story_pipeline.errors import StoryPipelineError
from story_pipeline.execution_store import (
    persist_finished_execution,
    persist_new_execution,
    persist_resumed_execution,
    persist_run_progress,
)
from story_pipeline.next_request import NEXT_REQUEST_TEMPLATE, create_next_request
from story_pipeline.persistence import atomic_write_json, atomic_write_text, sha256_file
from story_pipeline.run_lifecycle import (
    create_run_record,
    finalize_run_record,
    finish_step,
    record_model_attempt,
    resume_run_record,
    start_step,
)
from story_pipeline.run_report import (
    FileChange,
    HumanDecision,
    ReportContext,
    render_run_report,
    write_run_report,
)
from story_pipeline.runs import validate_run_data
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


NOW = "2026-07-22T01:23:45Z"
LATER = "2026-07-22T01:24:45Z"
COMMIT = "a" * 40


class PersistencePrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_json_is_formatted_and_hashes_raw_bytes(self) -> None:
        path = self.root / "value.json"
        atomic_write_json(path, {"日本語": [1, 2]})
        self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "日本語": [\n    1,\n    2\n  ]\n}\n')
        self.assertEqual(len(sha256_file(path)), 64)
        self.assertFalse(any(item.suffix == ".tmp" for item in self.root.iterdir()))

    def test_atomic_write_rejects_symlink_target(self) -> None:
        target = self.root / "target.txt"
        target.write_text("original", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to(target)
        with self.assertRaises(StoryPipelineError):
            atomic_write_text(link, "changed")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")


class RunLifecycleTests(unittest.TestCase):
    def make_run(self) -> dict[str, object]:
        return create_run_record(0, "b" * 64, COMMIT, now=NOW)

    def test_records_steps_calls_and_completed_status(self) -> None:
        run = start_step(self.make_run(), "interpret_request", input_hashes={"requests/0000.md": "b" * 64}, now=NOW)
        run = finish_step(run, "interpret_request", "completed", result="continue", now=LATER)
        run = record_model_attempt(
            run,
            category="generation",
            role="planner",
            model_reference="fast",
            api_model="deepseek/example",
            started_at=NOW,
            finished_at=LATER,
            result="success",
            attempts=1,
        )
        run = finalize_run_record(run, "completed", now=LATER)
        self.assertEqual(run["call_counts"]["generation"], 1)
        self.assertEqual(run["steps"][0]["status"], "completed")
        self.assertEqual(validate_run_data(run, 0), run)

    def test_records_detailed_transport_usage_and_validates_metrics(self) -> None:
        run = record_model_attempt(
            self.make_run(),
            category="generation",
            role="writer",
            model_reference="fast",
            api_model="deepseek/example",
            started_at=NOW,
            finished_at=LATER,
            result="completed",
            attempts=2,
            transport_attempts=(
                {
                    "model_reference": "fast", "api_model": "deepseek/example",
                    "attempt": 1, "maximum_attempts": 2, "started_at": NOW,
                    "finished_at": NOW, "elapsed_ms": 100, "result": "failed",
                    "failure_kind": "temporary", "wait_ms": 500,
                },
                {
                    "model_reference": "fast", "api_model": "deepseek/example",
                    "attempt": 2, "maximum_attempts": 2, "started_at": NOW,
                    "finished_at": LATER, "elapsed_ms": 200, "result": "completed",
                    "failure_kind": None, "wait_ms": 0,
                },
            ),
            usage={
                "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                "cached_tokens": None, "reasoning_tokens": 5, "cost_usd": 0.0025,
            },
        )

        self.assertEqual(run["metrics"]["transport_attempts"], 2)
        self.assertEqual(run["metrics"]["retry_wait_ms"], 500)
        self.assertEqual(run["metrics"]["usage"]["total_tokens"], 30)
        self.assertEqual(run["metrics"]["usage"]["cost_usd"], 0.0025)
        self.assertEqual(validate_run_data(run, 0), run)
        run["metrics"]["usage"]["total_tokens"] = 31
        with self.assertRaises(ValueError):
            validate_run_data(run, 0)

    def test_failed_run_can_resume_without_resetting_counters(self) -> None:
        run = self.make_run()
        run = finalize_run_record(
            run,
            "failed",
            resume_step="generate",
            resume_reason="temporary",
            error={"step": "generate", "category": "temporary", "message": "一時障害", "retryable": True},
            now=LATER,
        )
        resumed = resume_run_record(run, step="generate", reason="retry", now="2026-07-22T01:25:45Z")
        self.assertEqual(resumed["started_at"], NOW)
        self.assertEqual(resumed["status"], "running")
        self.assertIsNone(resumed["finished_at"])
        self.assertEqual(resumed["resume_count"], 1)
        self.assertEqual(resumed["request_revisions"][0]["sha256"], "b" * 64)

    def test_request_revision_updates_current_hash_and_preserves_initial_boundary(self) -> None:
        run = finalize_run_record(
            self.make_run(), "failed", resume_step="generate", resume_reason="temporary", now=LATER
        )
        resumed = resume_run_record(
            run,
            step="interpret_request",
            reason="要求が改訂されました",
            request_sha256="c" * 64,
            input_commit="d" * 40,
            now="2026-07-22T01:25:45Z",
        )

        self.assertEqual(resumed["schema_version"], 3)
        self.assertEqual(resumed["request_sha256"], "c" * 64)
        self.assertEqual(resumed["start_commit"], COMMIT)
        self.assertEqual(resumed["started_at"], NOW)
        self.assertEqual(resumed["request_revisions"][-1]["input_commit"], "d" * 40)

    def test_unchanged_resume_does_not_add_revision(self) -> None:
        run = finalize_run_record(
            self.make_run(), "failed", resume_step="generate", resume_reason="temporary", now=LATER
        )

        resumed = resume_run_record(
            run,
            step="generate",
            reason="retry",
            request_sha256="b" * 64,
            now="2026-07-22T01:25:45Z",
        )

        self.assertEqual(len(resumed["request_revisions"]), 1)
        self.assertEqual(resumed["current_step"], "generate")
        self.assertEqual(resumed["resume_count"], 1)

    def test_version_one_record_is_migrated_without_losing_initial_values(self) -> None:
        for index, marker in enumerate(("b", "c", "d")):
            with self.subTest(fixture=index):
                request_hash = marker * 64
                input_commit = marker * 40
                run = create_run_record(index, request_hash, input_commit, now=NOW)
                run["schema_version"] = 1
                run.pop("request_revisions")
                run.pop("resume_count")
                for key in ("model_calls", "events", "incidents", "lifecycle", "metrics"):
                    run.pop(key)

                migrated = validate_run_data(run, index)

                self.assertEqual(migrated["schema_version"], 3)
                self.assertEqual(migrated["request_revisions"][0]["sha256"], request_hash)
                self.assertEqual(migrated["request_revisions"][0]["input_commit"], input_commit)
                self.assertEqual(migrated["resume_count"], 0)

    def test_cannot_finalize_with_running_step(self) -> None:
        run = start_step(self.make_run(), "generate", now=NOW)
        with self.assertRaises(ValueError):
            finalize_run_record(run, "completed", now=LATER)


class ExecutionPersistenceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        create_scaffold(self.root)
        self.request = self.root / "requests" / "0000.md"
        self.request.write_text("# 作品作成要求\n\n物語を作る。\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scaffold_contains_runs_directory(self) -> None:
        self.assertTrue((self.root / ".story-pipeline" / "runs").is_dir())

    def test_start_progress_and_finish_persist_valid_records(self) -> None:
        state = load_state(self.root)
        request_hash = sha256_file(self.request)
        run = create_run_record(0, request_hash, COMMIT, now=NOW)
        state = persist_new_execution(self.root, state, run, now=NOW)
        self.assertEqual(load_state(self.root)["active_request"], 0)

        run = start_step(run, "interpret_request", input_hashes={"requests/0000.md": request_hash}, now=NOW)
        run = finish_step(run, "interpret_request", "completed", result="構想要求", now=LATER)
        persist_run_progress(self.root, run)
        run = finalize_run_record(run, "completed", now=LATER)
        state = persist_finished_execution(
            self.root,
            state,
            run,
            state_updates={"phase": "foundation"},
            now=LATER,
        )

        saved_run = json.loads((self.root / ".story-pipeline/runs/0000.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_run_data(saved_run, 0), saved_run)
        self.assertEqual(state["last_request"], 0)
        self.assertIsNone(state["active_request"])
        self.assertEqual(load_state(self.root)["phase"], "foundation")

    def test_pair_is_saved_in_run_then_state_order(self) -> None:
        state = load_state(self.root)
        run = create_run_record(0, sha256_file(self.request), COMMIT, now=NOW)
        paths: list[str] = []

        def record(path: Path, value: object) -> None:
            paths.append(path.relative_to(self.root).as_posix())

        with patch("story_pipeline.execution_store.atomic_write_json", side_effect=record):
            persist_new_execution(self.root, state, run, now=NOW)
        self.assertEqual(paths, [".story-pipeline/runs/0000.json", ".story-pipeline/state.json"])

    def test_failed_execution_remains_active_and_can_resume(self) -> None:
        state = load_state(self.root)
        run = create_run_record(0, sha256_file(self.request), COMMIT, now=NOW)
        state = persist_new_execution(self.root, state, run, now=NOW)
        failed = finalize_run_record(
            run, "failed", resume_step="generate", resume_reason="temporary", now=LATER
        )
        state = persist_finished_execution(self.root, state, failed, now=LATER)
        self.assertEqual(state["active_request"], 0)
        resumed = resume_run_record(failed, step="generate", reason="retry", now=LATER)
        state = persist_resumed_execution(self.root, state, resumed, now=LATER)
        self.assertEqual(state["active_request"], 0)


class ReportingAndNextRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        create_scaffold(self.root)
        self.run = finalize_run_record(
            create_run_record(0, "b" * 64, COMMIT, now=NOW), "completed", now=LATER
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_report_contains_every_required_section_and_facts(self) -> None:
        context = ReportContext(
            request_summary="物語を続ける",
            kind="continue",
            targets=("episode 1",),
            changed_files=(FileChange("episodes/0001.md", "作成"),),
            adoption_reason="整合性 error なし",
            story_changes=("主人公が旅立った",),
            decisions=(HumanDecision("decision-01", "結末を変えるか", "plot に影響", ("維持", "変更")),),
            next_action="第2話を計画する",
        )
        report = render_run_report(self.run, context)
        for heading in (
            "受け付けた要求", "要求の解釈", "実行した処理", "作成・変更したファイル",
            "評価と改稿", "物語・設定上の変更", "未解決の問題", "人間に判断してほしい事項",
            "次回の標準処理",
        ):
            self.assertIn(f"## {heading}", report)
        self.assertIn("episodes/0001.md", report)
        self.assertEqual(write_run_report(self.root, self.run, context), "requests/0000_agent.md")

    def test_report_keeps_missing_usage_unknown_and_shows_zero_resume_regeneration(self) -> None:
        run = dict(self.run)
        run["resume_count"] = 1
        report = render_run_report(
            run,
            ReportContext(request_summary="再開", kind="continue"),
        )

        self.assertIn("total_tokens=unknown", report)
        self.assertIn("cost_usd=unknown", report)
        self.assertIn("再開後 knowledge 再生成: 0", report)
        self.assertIn("retry_wait_ms=0", report)

    def test_next_request_uses_maximum_request_or_report_number(self) -> None:
        (self.root / "requests/0001_agent.md").write_text("report", encoding="utf-8")
        relative = create_next_request(self.root)
        self.assertEqual(relative, "requests/0002.md")
        self.assertEqual((self.root / relative).read_text(encoding="utf-8"), NEXT_REQUEST_TEMPLATE)

    def test_next_request_rejects_numbered_symlink(self) -> None:
        (self.root / "requests/0001.md").symlink_to(self.root / "requests/0000.md")
        with self.assertRaises(StoryPipelineError):
            create_next_request(self.root)
