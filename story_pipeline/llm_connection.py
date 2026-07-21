"""作品ファイル変更前に行う provider 初期接続確認。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from story_pipeline.llm_client import LLMClient


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    provider: str
    model_reference: str
    attempts: int


def check_initial_connections(
    config: dict[str, Any],
    environment: Mapping[str, str],
    model_references: list[str] | tuple[str, ...],
    *,
    client: LLMClient | None = None,
) -> tuple[ConnectionCheck, ...]:
    """必要な provider ごとに最初に使うモデルを一度だけ確認する。"""
    llm = client or LLMClient(config, environment)
    checked: set[str] = set()
    results: list[ConnectionCheck] = []
    for reference in model_references:
        model = config["models"][reference]
        provider = model["provider"]
        if provider in checked:
            continue
        attempts = llm.probe_model(reference)
        results.append(ConnectionCheck(provider, reference, attempts))
        checked.add(provider)
    return tuple(results)
