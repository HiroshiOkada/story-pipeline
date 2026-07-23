"""指定された OpenRouter 構成による手動接続試験。"""

from __future__ import annotations

from pathlib import Path

from story_pipeline.environment import load_environment
from story_pipeline.llm_client import LLMClient


def main() -> int:
    config = {
        "dotenv": {"files": [str(Path.home() / ".env")]},
        "providers": {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_APIKEY",
            }
        },
        "models": {
            "integration": {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-flash:nitro",
                "max_tokens": 32,
                "parameters": {"temperature": 0},
            }
        },
        "roles": {"writer": ["integration"]},
        "limits": {"retry_calls_per_request": 2},
        "request": {"timeout_seconds": 120, "retry_attempts": 2},
    }
    environment = load_environment(config)
    client = LLMClient(config, environment)
    chat_attempts = client.probe_model("integration")
    structured_attempts = client.probe_structured_output("integration")
    print(
        "OpenRouter capability check passed: "
        "model=deepseek/deepseek-v4-flash:nitro "
        f"chat_attempts={chat_attempts} structured_attempts={structured_attempts}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
