"""OpenRouter を使う全体構成制作フェーズの手動統合試験。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from story_pipeline.environment import load_environment
from story_pipeline.llm_client import LLMClient
from story_pipeline.plotting import DEFAULT_PLOTTING_CONTEXT
from story_pipeline.plotting_workflow import produce_plotting
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        create_scaffold(root)
        (root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n採用済みの構想と基礎設定を維持し、全体構成を作成してください。"
            "約8,000字の一話完結短編として、友情の再生を結末まで描き、超常現象と夢落ちは"
            "使わないでください。\n",
            encoding="utf-8",
        )
        documents = {
            "concept.md": "# 潮待ちの夏\n\n現代日本の海辺で、疎遠になった友人同士が共同作業を通じて再出発する約8,000字の青春短編。\n",
            "world.md": "# 世界設定\n\n現代日本の海辺の町。超常現象は存在しない。夏祭り前の一日を描く。\n",
            "characters.md": "# 人物設定\n\n高校生の凪と湊。二人は過去の口論で疎遠だが、壊れた祭りの看板を一緒に直す。\n",
            "style.md": "# 文体設定\n\n凪の三人称限定視点、過去時制。簡潔で感覚的な青春小説の文体。\n",
            "canon.md": "# 確定事実\n\n物語開始時、凪と湊は疎遠であり、祭りの看板は壊れている。\n",
        }
        for path in DEFAULT_PLOTTING_CONTEXT:
            (root / path).write_text(documents[path], encoding="utf-8")
        request = select_request(root, load_state(root))
        interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "continue",
                    "summary": "青春短編の全体構成を作る",
                    "targets": [],
                    "required_conditions": ["約8,000字", "一話完結", "友情の再生"],
                    "prohibited_changes": ["超常現象", "夢落ち"],
                    "additional_material": [],
                    "decision_answers": [],
                    "ambiguities": [],
                    "requested_units": 1,
                    "requested_until": None,
                },
                ensure_ascii=False,
            ),
            request.content,
        )
        config = {
            "dotenv": {"files": [str(Path.home() / ".env")]},
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_APIKEY",
                }
            },
            "models": {
                "plotting-integration": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "max_tokens": 8192,
                    "parameters": {"temperature": 0},
                }
            },
            "roles": {
                role: ["plotting-integration"] for role in ("writer", "reviewer", "reviser")
            },
            "limits": {
                "generation_calls": 3,
                "review_calls": 3,
                "revision_calls": 3,
                "retry_calls_per_request": 2,
            },
            "request": {"timeout_seconds": 120, "retry_attempts": 2},
        }
        result = produce_plotting(
            root,
            request,
            interpretation,
            LLMClient(config, load_environment(config)),
        )
        if result.status != "completed" or result.best is None:
            raise RuntimeError(
                f"plotting workflow did not complete: {result.status} {result.reason}"
            )
        if not result.best.candidate.chapters:
            raise RuntimeError("adopted plotting candidate has no chapter plan")
        print(
            "OpenRouter plotting production passed: "
            f"model={result.best.candidate.model_reference} "
            f"chapters={len(result.best.candidate.chapters)} calls={dict(result.call_counts)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
