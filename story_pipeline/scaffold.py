"""初期 scaffold のテンプレートと安全な作成処理。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


CONFIG_TEMPLATE = """{
  "config_version": 1,
  "dotenv": {
    "files": ["~/.env", ".env"]
  },
  "providers": {
    "openai": {
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY"
    }
  },
  "models": {
    "default": {
      "provider": "openai",
      "model": "gpt-4.1"
    }
  },
  "roles": {
    "planner": ["default"],
    "writer": ["default"],
    "reviewer": ["default"],
    "reviser": ["default"],
    "summarizer": ["default"]
  },
  "limits": {
    "generation_calls": 3,
    "review_calls": 3,
    "revision_calls": 3,
    "summary_calls": 3,
    "retry_calls_per_request": 2,
    "max_changed_lines": 999
  },
  "request": {
    "timeout_seconds": 120,
    "retry_attempts": 3
  }
}
"""

INITIAL_REQUEST_TEMPLATE = """# 作品作成要求

## 作りたい作品

<!-- ジャンル、着想、読者、長さなどを記述してください。 -->

## 必須条件

<!-- 必ず守る内容を記述してください。 -->

## 禁止事項

<!-- 含めてほしくない内容を記述してください。 -->
"""

GITIGNORE_TEMPLATE = ".env\n.story-pipeline/run.lock\n"

SCAFFOLD_FILE_PATHS = (
    ".gitignore",
    ".story-pipeline/state.json",
    "requests/0000.md",
    "story-pipeline-config.jsonc",
)


def _state_template() -> str:
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    state = {
        "schema_version": 1,
        "phase": "concept",
        "next_chapter": 1,
        "next_episode": 1,
        "completed_chapters": [],
        "completed_episodes": [],
        "current_chapter": None,
        "pending_reviews": [],
        "pending_decisions": [],
        "last_request": None,
        "active_request": None,
        "updated_at": now,
    }
    return json.dumps(state, ensure_ascii=False, indent=2) + "\n"


def create_scaffold(root: Path) -> None:
    """scaffold を作り、失敗時には今回作成したパスだけを戻す。"""
    directories = (
        root / "requests",
        root / "chapters",
        root / "episode_plans",
        root / "episodes",
        root / ".story-pipeline",
        root / ".story-pipeline" / "runs",
    )
    contents = {
        "story-pipeline-config.jsonc": CONFIG_TEMPLATE,
        "requests/0000.md": INITIAL_REQUEST_TEMPLATE,
        ".story-pipeline/state.json": _state_template(),
        ".gitignore": GITIGNORE_TEMPLATE,
    }
    created: list[Path] = []
    try:
        for directory in directories:
            directory.mkdir()
            created.append(directory)
        for relative in SCAFFOLD_FILE_PATHS:
            path = root / relative
            content = contents[relative]
            path.write_text(content, encoding="utf-8", newline="\n")
            created.append(path)
    except BaseException:
        for path in reversed(created):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        raise
