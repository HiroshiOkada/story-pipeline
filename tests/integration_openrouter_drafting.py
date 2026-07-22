"""OpenRouter を使う本文制作フェーズの手動統合試験。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from story_pipeline.drafting import DEFAULT_DRAFTING_CONTEXT
from story_pipeline.drafting_workflow import produce_draft
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
        (root / "requests" / "0000.md").write_text(
            "# 作品作成要求\n\n採用済み設定と計画を維持し、約400字の一話完結短編を執筆してください。"
            "壊れた祭りの看板を直す共同作業を通じて凪と湊の友情が再生する話にし、"
            "超常現象と夢落ちは使わないでください。\n",
            encoding="utf-8",
        )
        documents = {
            "concept.md": "# 潮待ちの夏\n\n現代日本の海辺で、疎遠になった友人同士が共同作業を通じて再出発する青春短編。\n",
            "world.md": "# 世界設定\n\n現代日本の海辺の町。夏祭り前日の午後から夕方。超常現象は存在しない。\n",
            "characters.md": "# 人物設定\n\n凪は慎重な高校生。湊は率直な幼なじみ。過去の口論以来、二人は疎遠である。\n",
            "plot.md": "# 全体構成\n\n二人が壊れた看板の修理を通じて口論の原因を認め、友情を再開する一話完結。\n",
            "style.md": "# 文体設定\n\n凪の三人称限定視点、過去時制。簡潔で感覚的。説明過多を避ける。\n",
            "canon.md": "# 確定事実\n\n開始時、凪と湊は疎遠で、祭りの木製看板は壊れている。\n",
        }
        for path in DEFAULT_DRAFTING_CONTEXT:
            (root / path).write_text(documents[path], encoding="utf-8")
        (root / "episode_plans" / "0001.md").write_text(
            "# 第1話計画\n\n## 目的\n共同修理を通じて友情を再生する。\n\n"
            "## 登場人物\n凪、湊。\n\n## 開始状態\n二人は疎遠で、祭りの看板は壊れている。\n\n"
            "## 終了状態\n看板の修理が完了し、二人は翌日の祭りで会う約束をする。\n\n"
            "## 場面\n倉庫前で再会する。反発しながら修理する。口論の原因を話す。看板を掲げる。\n\n"
            "## 開示情報\n口論は互いに相手へ期待していたため起きた。\n\n"
            "## 感情変化\n警戒から苛立ち、理解、安堵へ変化する。\n\n"
            "## 伏線\nなし。\n\n## 次話への引き\nなし（一話完結）。\n\n## 目標文字数\n400字\n",
            encoding="utf-8",
        )
        request = select_request(root, load_state(root))
        interpretation = parse_request_interpretation(json.dumps({
            "kind": "continue", "summary": "友情再生を描く第1話を執筆する", "targets": [],
            "required_conditions": ["約400字", "一話完結", "友情の再生", "看板の共同修理"],
            "prohibited_changes": ["超常現象", "夢落ち"], "additional_material": [],
            "decision_answers": [], "ambiguities": [], "requested_units": 1,
            "requested_until": None,
        }, ensure_ascii=False), request.content)
        config = {
            "dotenv": {"files": [str(Path.home() / ".env")]},
            "providers": {"openrouter": {
                "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_APIKEY",
            }},
            "models": {"drafting-integration": {
                "provider": "openrouter", "model": "deepseek/deepseek-v4-flash",
                "max_tokens": 2048, "parameters": {"temperature": 0},
            }},
            "roles": {
                role: ["drafting-integration"] for role in ("writer", "reviewer", "reviser")
            },
            "limits": {
                "generation_calls": 3, "review_calls": 5, "revision_calls": 3,
                "retry_calls_per_request": 2,
            },
            "request": {"timeout_seconds": 120, "retry_attempts": 2},
        }
        result = produce_draft(
            root, request, interpretation, 1, LLMClient(config, load_environment(config))
        )
        if result.status != "completed" or result.best is None or result.knowledge_update is None:
            raise RuntimeError(f"drafting workflow did not complete: {result.status} {result.reason}")
        if result.best.candidate.path != "episodes/0001.md":
            raise RuntimeError("adopted draft has an unexpected target path")
        print(
            "OpenRouter drafting production passed: "
            f"model={result.best.candidate.model_reference} "
            f"facts={len(result.knowledge_update.canon_facts)} "
            f"states={len(result.knowledge_update.character_states)} "
            f"calls={dict(result.call_counts)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
