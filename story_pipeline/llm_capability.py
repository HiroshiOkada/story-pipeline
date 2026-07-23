"""設定された LLM の最低限の実行能力を明示的に検査する。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from story_pipeline.config import load_config
from story_pipeline.environment import load_environment
from story_pipeline.llm_client import LLMClient
from story_pipeline.llm_transport import ApiFailure
from story_pipeline.project import find_project_root


EXIT_API = 7


def check_llm_command(
    output: TextIO,
    error_output: TextIO,
    *,
    root: Path | None = None,
    client: LLMClient | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """全利用モデルの通常応答と JSON Schema 応答を検査する。"""
    project_root = find_project_root(root)
    config = load_config(project_root)
    loaded_environment = load_environment(config) if environment is None else environment
    llm = client or LLMClient(config, loaded_environment)
    references = tuple(dict.fromkeys(
        reference
        for role in sorted(config["roles"])
        for reference in config["roles"][role]
    ))
    failures = 0
    for reference in references:
        model = config["models"][reference]
        label = f"{reference} ({model['provider']}/{model['model']})"
        try:
            llm.probe_model(reference)
            print(f"PASS {label}: chat completion", file=output)
            llm.probe_structured_output(reference)
            print(f"PASS {label}: structured JSON", file=output)
        except ApiFailure as failure:
            failures += 1
            print(f"FAIL {label}: {failure.kind}: {failure.message}", file=error_output)
    if failures:
        print(f"LLM capability check failed: {failures} model(s).", file=error_output)
        return EXIT_API
    print(f"LLM capability check passed: {len(references)} model(s).", file=output)
    return 0
