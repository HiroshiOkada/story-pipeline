"""OpenRouter を使う構想制作フェーズの手動統合試験。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from story_pipeline.concept_workflow import produce_concept
from story_pipeline.environment import load_environment
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
            "現代日本の海辺を舞台に、友情と再出発をテーマにした一般読者向けの"
            "約8,000字の青春短編を新規作成してください。夢落ちは禁止します。"
            "細部は合理的に仮定して構いません。\n",
            encoding="utf-8",
        )
        state = load_state(root)
        request = select_request(root, state)
        interpretation = parse_request_interpretation(
            json.dumps(
                {
                    "kind": "create",
                    "summary": "海辺を舞台にした青春短編の構想を作る",
                    "targets": [],
                    "required_conditions": [
                        "現代日本の海辺",
                        "友情と再出発",
                        "一般読者向け",
                        "約8,000字",
                    ],
                    "prohibited_changes": ["夢落ち"],
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
                "concept-integration": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "max_tokens": 4096,
                    "parameters": {"temperature": 0},
                }
            },
            "roles": {
                role: ["concept-integration"]
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
        environment = load_environment(config)
        result = produce_concept(
            root,
            request,
            interpretation,
            LLMClient(config, environment),
        )
        if result.status != "completed" or result.best is None:
            raise RuntimeError(f"concept workflow did not complete: {result.status} {result.reason}")
        if "## タイトル" not in result.best.candidate.content:
            raise RuntimeError("adopted concept has no title section")
        print(
            "OpenRouter concept production passed: "
            f"model={result.best.candidate.model_reference} calls={dict(result.call_counts)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
