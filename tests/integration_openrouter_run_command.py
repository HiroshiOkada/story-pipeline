"""OpenRouter を使う `run` コマンド全体の手動統合確認。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile

from story_pipeline.run_command import run_command
from story_pipeline.scaffold import create_scaffold


MODEL = "deepseek/deepseek-v4-flash:nitro"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        create_scaffold(root)
        config_path = root / "story-pipeline-config.jsonc"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["dotenv"]["files"] = [str(Path("~/.env").expanduser())]
        config["providers"] = {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_APIKEY",
            }
        }
        config["models"] = {
            "default": {
                "provider": "openrouter",
                "model": MODEL,
                "max_tokens": 8192,
                "parameters": {"temperature": 0, "reasoning": {"effort": "none"}},
            }
        }
        for role in config["roles"]:
            config["roles"][role] = ["default"]
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        request = root / "requests/0000.md"
        request.write_text(
            "# 作品作成要求\n\n`concept.md` に、現代日本の海辺を舞台とする、忘れ物を届ける高校生の短編小説の構想を作ってください。"
            "読後感は穏やかにし、暴力描写を避けてください。\n",
            encoding="utf-8",
        )
        _git(root, "init", "-q")
        _git(root, "config", "user.name", "Story Pipeline Integration")
        _git(root, "config", "user.email", "integration@example.invalid")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", "Initial")
        previous = Path.cwd()
        try:
            os.chdir(root)
            output = io.StringIO()
            errors = io.StringIO()
            code = run_command(output=output, error_output=errors)
        finally:
            os.chdir(previous)
        print(output.getvalue(), end="")
        print(errors.getvalue(), end="")
        if code != 0:
            raise RuntimeError(f"run command integration failed with exit code {code}")
        run = json.loads((root / ".story-pipeline/runs/0000.json").read_text(encoding="utf-8"))
        if run["status"] != "completed" or not (root / "concept.md").is_file():
            raise RuntimeError("run command did not adopt a completed concept")
        models = {item["api_model"] for item in run["model_attempts"]}
        if models != {MODEL}:
            raise RuntimeError(f"unexpected models recorded: {sorted(models)}")
        status = _git(root, "status", "--short").stdout.decode()
        if status != "?? requests/0001.md\n":
            raise RuntimeError(f"unexpected final worktree: {status!r}")
    return 0


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
