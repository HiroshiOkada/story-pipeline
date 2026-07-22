"""OpenRouter を使う要求解釈の手動統合試験。"""

from __future__ import annotations

from pathlib import Path
import tempfile

from story_pipeline.environment import load_environment
from story_pipeline.llm_client import LLMClient
from story_pipeline.request_planner import plan_selected_request
from story_pipeline.request_selection import select_request
from story_pipeline.scaffold import create_scaffold
from story_pipeline.state import load_state


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        create_scaffold(root)
        (root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n"
            "現代日本を舞台にした一般読者向けの約8,000字の青春短編を新規作成してください。"
            "中心テーマは友情と再出発です。対象ファイルは `concept.md` です。"
            "その他の細部は合理的に仮定してよく、人間への確認事項はありません。\n",
            encoding="utf-8",
        )
        state = load_state(root)
        request = select_request(root, state)
        if request is None:
            raise RuntimeError("test request was not selected")
        config = {
            "dotenv": {"files": [str(Path.home() / ".env")]},
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_APIKEY",
                }
            },
            "models": {
                "planner-integration": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "max_tokens": 1024,
                    "parameters": {"temperature": 0},
                }
            },
            "roles": {"planner": ["planner-integration"]},
            "limits": {"retry_calls_per_request": 2, "generation_calls": 3},
            "request": {"timeout_seconds": 120, "retry_attempts": 2},
        }
        environment = load_environment(config)
        planned = plan_selected_request(root, state, request, LLMClient(config, environment))
        if planned.interpretation.kind != "create" or planned.scope.action != "create_concept":
            raise RuntimeError("unexpected interpretation or scope")
        print(
            "OpenRouter interpretation passed: "
            f"kind={planned.interpretation.kind} scope={planned.scope.action} "
            f"attempts={planned.completion.attempts}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
