"""OpenRouter を使う話計画制作フェーズの手動統合試験。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from story_pipeline.environment import load_environment
from story_pipeline.episode_planning import DEFAULT_EPISODE_PLANNING_CONTEXT
from story_pipeline.episode_planning_workflow import produce_episode_plan
from story_pipeline.llm_client import LLMClient
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        create_scaffold(root)
        (root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n採用済みの構想、設定、全体構成を維持し、第1話を計画してください。"
            "全体約8,000字の一話完結短編として、壊れた祭りの看板を直す共同作業を通じた友情の"
            "再生を描き、超常現象と夢落ちは使わないでください。\n",
            encoding="utf-8",
        )
        documents = {
            "concept.md": "# 潮待ちの夏\n\n現代日本の海辺で、疎遠になった友人同士が共同作業を通じて再出発する約8,000字の青春短編。\n",
            "world.md": "# 世界設定\n\n現代日本の海辺の町。超常現象は存在しない。夏祭り前の一日を描く。\n",
            "characters.md": "# 人物設定\n\n高校生の凪と湊。二人は過去の口論で疎遠だが、壊れた祭りの看板を一緒に直す。\n",
            "style.md": "# 文体設定\n\n凪の三人称限定視点、過去時制。簡潔で感覚的な青春小説の文体。\n",
            "canon.md": "# 確定事実\n\n物語開始時、凪と湊は疎遠であり、祭りの看板は壊れている。\n",
            "plot.md": "# 全体構成\n\n再会した二人が反発しながら看板を修理し、過去の口論を認め合って再出発する。\n",
        }
        for path in DEFAULT_EPISODE_PLANNING_CONTEXT:
            (root / path).write_text(documents[path], encoding="utf-8")
        (root / "chapters" / "0001.md").write_text(
            "# 第一章\n\n## 目的\n共同作業を通じて友情を再生する。\n\n"
            "## 開始状態\n凪と湊は疎遠で、看板は壊れている。\n\n"
            "## 終了状態\n二人は過去の口論を認め、看板を直して再出発する。\n\n"
            "## 主要な出来事\n再会、衝突、共同修理、和解。\n\n"
            "## 収録話\n0001\n\n## 接続条件\n一話完結。\n\n"
            "## 完成後のあらすじ\n未完成。\n",
            encoding="utf-8",
        )
        request = select_request(root, load_state(root))
        interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "continue", "summary": "青春短編の第1話を計画する", "targets": [],
                    "required_conditions": ["約8,000字", "一話完結", "友情の再生", "看板を直す共同作業"],
                    "prohibited_changes": ["超常現象", "夢落ち"], "additional_material": [],
                    "decision_answers": [], "ambiguities": [], "requested_units": 1,
                    "requested_until": None,
                },
                ensure_ascii=False,
            ),
            request.content,
        )
        config = {
            "dotenv": {"files": [str(Path.home() / ".env")]},
            "providers": {"openrouter": {
                "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_APIKEY",
            }},
            "models": {"episode-planning-integration": {
                "provider": "openrouter", "model": "deepseek/deepseek-v4-flash",
                "max_tokens": 8192, "parameters": {"temperature": 0},
            }},
            "roles": {
                role: ["episode-planning-integration"]
                for role in ("planner", "reviewer", "reviser")
            },
            "limits": {
                "generation_calls": 3, "review_calls": 3, "revision_calls": 3,
                "retry_calls_per_request": 2,
            },
            "request": {"timeout_seconds": 120, "retry_attempts": 2},
        }
        result = produce_episode_plan(
            root, request, interpretation, 1, LLMClient(config, load_environment(config))
        )
        if result.status != "completed" or result.best is None:
            raise RuntimeError(
                f"episode planning workflow did not complete: {result.status} {result.reason}"
            )
        if result.best.candidate.path != "episode_plans/0001.md":
            raise RuntimeError("adopted episode plan has an unexpected target path")
        print(
            "OpenRouter episode planning production passed: "
            f"model={result.best.candidate.model_reference} calls={dict(result.call_counts)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
