"""wheel で固定3作品を順次実行する OpenRouter 手動統合試験。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from story_pipeline.production_validation import (
    StoryMeasurement,
    measure_run,
    may_start_next_story,
    story_failures,
)


MODEL = "deepseek/deepseek-v4-flash:nitro"
STORIES = (
    "海辺の町で忘れ物を届ける高校生を主人公とする、1章1話・本文2,000〜3,000字の穏やかな読切。暴力描写は禁止。",
    "雪の山村で古いラジオを修理する姉弟を主人公とする、1章1話・本文2,000〜3,000字の希望のある読切。超自然的解決は禁止。",
    "宇宙港の植物園で最後の花を守る整備士を主人公とする、1章1話・本文2,000〜3,000字の静かなSF読切。戦闘は禁止。",
)
PHASE_REQUESTS = {
    "foundation": "構想に基づき、世界設定、登場人物、文体、canon の次の標準処理を実行してください。",
    "plotting": "1章1話の読切として、プロットと第1章の計画を作成してください。",
    "episode_planning": "第1話の計画を、本文2,000〜3,000字で読切を完結させる条件で作成してください。",
    "drafting": "第1話の本文を2,000〜3,000字で執筆し、読切として完結させてください。",
    "chapter_revision": "現在の制作状態から次の標準処理を継続し、第1章の完了を判定してください。",
    "final_revision": "現在の制作状態から次の標準処理を継続し、読切作品全体の完成を判定してください。",
}


class StoryExecutionFailure(RuntimeError):
    def __init__(self, story: StoryMeasurement, diagnostic: dict[str, Any]) -> None:
        super().__init__(diagnostic["failure"])
        self.story = story
        self.diagnostic = diagnostic


def main(arguments: list[str] | None = None) -> int:
    values = sys.argv[1:] if arguments is None else arguments
    if len(values) != 2:
        raise RuntimeError("usage: integration_openrouter_three_stories.py WHEEL OUTPUT_JSON")
    wheel = Path(values[0]).resolve(strict=True)
    output = Path(values[1]).resolve()
    started = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    prior_cost = Decimal(os.environ.get("STORY_PIPELINE_VALIDATION_PRIOR_COST_USD", "0"))
    if prior_cost < 0 or prior_cost > Decimal("5.00"):
        raise RuntimeError("prior cost must be between USD 0 and USD 5.00")
    cumulative = prior_cost
    temporary = output.with_name(output.stem + "-work")
    temporary.mkdir(parents=True, exist_ok=False)
    command, environment = _install_wheel(temporary, wheel)
    for index, premise in enumerate(STORIES, 1):
        if not may_start_next_story(cumulative):
            break
        try:
            story = _execute_story(
                temporary / f"story-{index}", command, environment, premise
            )
        except StoryExecutionFailure as error:
            story = error.story
            cost = story.cost_usd
            results.append({
                "story": index,
                "passed": False,
                "failures": ["EXECUTION_FAILED"],
                "diagnostic": error.diagnostic,
                "wall_seconds": story.wall_seconds,
                "logical_calls": story.logical_calls,
                "transport_attempts": story.transport_attempts,
                "draft_writer_calls": story.draft_writer_calls,
                "cost_usd": None if cost is None else str(cost),
                "runs": _run_results(story),
            })
            if cost is not None:
                cumulative += cost
            break
        except Exception as error:
            results.append({
                "story": index,
                "passed": False,
                "failures": ["HARNESS_FAILED"],
                "diagnostic": {"exception_class": type(error).__name__},
                "wall_seconds": None,
                "logical_calls": 0,
                "transport_attempts": 0,
                "draft_writer_calls": 0,
                "cost_usd": None,
                "runs": [],
            })
            break
        failures = story_failures(story, cumulative_cost_before=cumulative)
        cost = story.cost_usd
        results.append({
            "story": index,
            "passed": not failures,
            "failures": list(failures),
            "wall_seconds": story.wall_seconds,
            "logical_calls": story.logical_calls,
            "transport_attempts": story.transport_attempts,
            "draft_writer_calls": story.draft_writer_calls,
            "cost_usd": None if cost is None else str(cost),
            "runs": _run_results(story),
        })
        if failures or cost is None:
            break
        cumulative += cost
    payload = {
        "schema_version": 1,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "model": MODEL,
        "stories_completed": len(results),
        "passed": len(results) == len(STORIES) and all(item["passed"] for item in results),
        "prior_cost_usd": str(prior_cost),
        "total_cost_usd": str(cumulative),
        "work_directory": str(temporary),
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if payload["passed"] else 1


def _execute_story(
    root: Path, command: Path, environment: dict[str, str], premise: str
) -> StoryMeasurement:
    root.mkdir()
    _run(command, "init", root, env=environment)
    _configure(root)
    measurements = []
    started = time.monotonic()
    for _ in range(8):
        state = _json(root / ".story-pipeline/state.json")
        phase = state["phase"]
        if phase == "completed":
            break
        request_number = _next_request_number(root)
        request = premise if phase == "concept" else PHASE_REQUESTS[phase]
        (root / "requests" / f"{request_number:04d}.md").write_text(
            f"# 作品作成要求\n\n{request}\n", encoding="utf-8"
        )
        completed = _run(
            command, "run", cwd=root, env=environment, timeout=20 * 60, check=False
        )
        run = _json(root / ".story-pipeline/runs" / f"{request_number:04d}.json")
        measurements.append(measure_run(run, phase))
        if completed.returncode != 0:
            raise StoryExecutionFailure(
                StoryMeasurement(tuple(measurements), time.monotonic() - started),
                _failure_diagnostic(root, run, phase, completed.returncode),
            )
        _run(command, "validate", cwd=root, env=environment)
        expected = f"?? requests/{request_number + 1:04d}.md\n"
        status = _run("git", "-C", root, "status", "--short").stdout
        if status != expected:
            raise RuntimeError(f"unexpected worktree after request {request_number:04d}: {status!r}")
    state = _json(root / ".story-pipeline/state.json")
    if state["phase"] != "completed":
        raise RuntimeError(f"story did not complete: phase={state['phase']}")
    return StoryMeasurement(tuple(measurements), time.monotonic() - started)


def _failure_diagnostic(
    root: Path, run: dict[str, Any], phase: str, exit_code: int
) -> dict[str, Any]:
    checkpoint_path = root / ".story-pipeline/checkpoints" / f"{run['request_number']:04d}" / "draft.json"
    checkpoint = _json(checkpoint_path) if checkpoint_path.is_file() else None
    return {
        "failure": f"story run failed: phase={phase} code={exit_code}",
        "phase": phase,
        "exit_code": exit_code,
        "run_status": run["status"],
        "current_step": run["current_step"],
        "resume": run["resume"],
        "steps": [
            {"id": item["id"], "status": item["status"], "result": item["result"]}
            for item in run["steps"]
        ],
        "errors": run["errors"],
        "incidents": run["incidents"],
        "checkpoint": (
            None if checkpoint is None else {
                "request_revision": checkpoint["request_revision"],
                "candidate_sha256": checkpoint["candidate"]["sha256"],
                "knowledge_status": checkpoint["knowledge"]["status"],
                "adoption_status": checkpoint["adoption"]["status"],
            }
        ),
    }


def _run_results(story: StoryMeasurement) -> list[dict[str, Any]]:
    return [
        asdict(item) | {"cost_usd": None if item.cost_usd is None else str(item.cost_usd)}
        for item in story.runs
    ]


def _configure(root: Path) -> None:
    path = root / "story-pipeline-config.jsonc"
    config = _json(path)
    config["dotenv"]["files"] = [str(Path.home() / ".env")]
    config["providers"] = {
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_APIKEY"}
    }
    config["models"] = {
        "default": {
            "provider": "openrouter", "model": MODEL, "max_tokens": 16384,
            "parameters": {"temperature": 0, "reasoning": {"effort": "none"}},
        }
    }
    for role in config["roles"]:
        config["roles"][role] = ["default"]
    config["request"] = {"timeout_seconds": 120, "retry_attempts": 2}
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _install_wheel(root: Path, wheel: Path) -> tuple[Path, dict[str, str]]:
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(root / "uv-cache")
    environment.update({
        "GIT_AUTHOR_NAME": "Story Pipeline Integration",
        "GIT_AUTHOR_EMAIL": "integration@example.invalid",
        "GIT_COMMITTER_NAME": "Story Pipeline Integration",
        "GIT_COMMITTER_EMAIL": "integration@example.invalid",
    })
    venv = root / "venv"
    _run("uv", "venv", "--python", sys.executable, venv, env=environment)
    executable = venv / ("Scripts" if os.name == "nt" else "bin")
    python = executable / ("python.exe" if os.name == "nt" else "python")
    command = executable / ("story-pipeline.exe" if os.name == "nt" else "story-pipeline")
    _run("uv", "pip", "install", "--python", python, "--no-deps", "--no-index", wheel, env=environment)
    return command, environment


def _next_request_number(root: Path) -> int:
    return max(int(path.stem) for path in (root / "requests").glob("[0-9][0-9][0-9][0-9].md"))


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(
    *arguments: str | Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(value) for value in arguments], cwd=cwd, env=env, timeout=timeout,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"command failed: {arguments} code={completed.returncode}")
    return completed


if __name__ == "__main__":
    raise SystemExit(main())
