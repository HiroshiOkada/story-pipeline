"""ビルド済み wheel の導入と CLI を実プロセスで確認する。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main(arguments: list[str] | None = None) -> int:
    values = sys.argv[1:] if arguments is None else arguments
    if len(values) != 1:
        raise RuntimeError("usage: integration_distribution.py PATH_TO_WHEEL")
    wheel = Path(values[0]).resolve(strict=True)
    if wheel.suffix != ".whl":
        raise RuntimeError(f"wheel ではありません: {wheel}")

    with tempfile.TemporaryDirectory() as directory:
        temporary_root = Path(directory)
        environment_root = temporary_root / "venv"
        install_environment = os.environ.copy()
        install_environment["UV_CACHE_DIR"] = str(temporary_root / "uv-cache")
        _run(
            "uv",
            "venv",
            "--python",
            sys.executable,
            environment_root,
            cwd=temporary_root,
            env=install_environment,
        )
        executable_directory = environment_root / (
            "Scripts" if os.name == "nt" else "bin"
        )
        python = executable_directory / ("python.exe" if os.name == "nt" else "python")
        command = executable_directory / (
            "story-pipeline.exe" if os.name == "nt" else "story-pipeline"
        )

        _run(
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "--no-deps",
            "--no-index",
            str(wheel),
            env=install_environment,
        )
        version = _run(command, "--version")
        if version.stdout != "story-pipeline 0.1.0\n":
            raise RuntimeError(f"想定外の version 出力です: {version.stdout!r}")

        project = temporary_root / "story"
        project.mkdir()
        initialized = _run(command, "init", str(project))
        if "Next request: requests/0000.md" not in initialized.stdout:
            raise RuntimeError(f"初期化出力が不正です: {initialized.stdout!r}")

        config_path = project / "story-pipeline-config.jsonc"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["dotenv"]["files"] = []
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _git(
            project,
            "add",
            ".gitignore",
            "story-pipeline-config.jsonc",
            "requests/0000.md",
            ".story-pipeline/state.json",
        )
        _git(
            project,
            "-c",
            "user.name=Story Pipeline Integration",
            "-c",
            "user.email=integration@example.invalid",
            "commit",
            "-q",
            "-m",
            "Initial project",
        )

        status = _run(command, "status", cwd=project)
        if (
            "Phase: concept\n" not in status.stdout
            or "Next episode: 0001\n" not in status.stdout
        ):
            raise RuntimeError(f"status 出力が不正です: {status.stdout!r}")

        process_environment = os.environ.copy()
        process_environment["OPENAI_API_KEY"] = "integration-test-only"
        validated = _run(command, "validate", cwd=project, env=process_environment)
        if validated.stdout != "Validation passed.\n":
            raise RuntimeError(f"validate 出力が不正です: {validated.stdout!r}")
    return 0


def _run(
    *arguments: str | Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(value) for value in arguments],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        command = " ".join(str(value) for value in arguments)
        raise RuntimeError(
            f"コマンドが失敗しました ({completed.returncode}): {command}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _run("git", "-C", root, *arguments)


if __name__ == "__main__":
    raise SystemExit(main())
