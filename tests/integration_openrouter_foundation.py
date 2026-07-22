"""OpenRouter を使う基礎設定制作フェーズの手動統合試験。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from story_pipeline.environment import load_environment
from story_pipeline.foundation import FOUNDATION_FILES
from story_pipeline.foundation_workflow import produce_foundation
from story_pipeline.llm_client import LLMClient
from story_pipeline.request_interpretation import parse_request_interpretation
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        create_scaffold(root)
        request_path = root / "requests" / "0000.md"
        request_path.write_text(
            "# 作品作成要求\n\n"
            "採用済み構想を維持し、基礎設定を作成してください。現代日本の海辺という舞台と、"
            "友情と再出発というテーマを守り、超常現象は登場させないでください。\n",
            encoding="utf-8",
        )
        (root / "concept.md").write_text(
            "# 潮待ちの夏\n\n"
            "## タイトル\n潮待ちの夏\n\n"
            "## ジャンル\n現代青春ドラマ\n\n"
            "## 想定読者\n一般読者\n\n"
            "## 中心的な着想\n海辺の町で疎遠になった友人同士が共同作業を通して再出発する。\n\n"
            "## テーマ\n友情と再出発\n\n"
            "## 規模\n約8,000字の短編\n\n"
            "## 連載方針\n一話完結\n\n"
            "## 必須条件\n現代日本の海辺を舞台にする。\n\n"
            "## 禁止事項\n夢落ちと超常現象を使わない。\n\n"
            "## 仮定\n細部の地名と人物名は制作時に定める。\n",
            encoding="utf-8",
        )
        request = select_request(root, load_state(root))
        interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "continue",
                    "summary": "採用済み構想から基礎設定を作る",
                    "targets": [],
                    "required_conditions": ["現代日本の海辺", "友情と再出発"],
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
                "foundation-integration": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "max_tokens": 8192,
                    "parameters": {"temperature": 0},
                }
            },
            "roles": {
                role: ["foundation-integration"]
                for role in ("writer", "reviewer", "reviser")
            },
            "limits": {
                "generation_calls": 3,
                "review_calls": 3,
                "revision_calls": 3,
                "retry_calls_per_request": 2,
            },
            "request": {"timeout_seconds": 120, "retry_attempts": 2},
        }
        result = produce_foundation(
            root,
            request,
            interpretation,
            LLMClient(config, load_environment(config)),
        )
        if result.status != "completed" or result.best is None:
            raise RuntimeError(
                f"foundation workflow did not complete: {result.status} {result.reason}"
            )
        if tuple(name for name, _ in result.best.candidate.documents) != FOUNDATION_FILES:
            raise RuntimeError("adopted foundation does not contain the complete bundle")
        print(
            "OpenRouter foundation production passed: "
            f"model={result.best.candidate.model_reference} calls={dict(result.call_counts)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
