"""OpenRouter を使う章改稿フェーズの手動統合試験。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from story_pipeline.chapter_revision import DEFAULT_CHAPTER_REVISION_CONTEXT
from story_pipeline.chapter_revision_workflow import produce_chapter_revision
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
            "# 作品作成要求\n\n採用済みの短編第1章を評価し、必要な箇所だけ局所改稿して完成させてください。"
            "凪と湊が祭りの看板を直して友情を取り戻す筋、現代日本、三人称限定視点を維持し、"
            "超常現象と夢落ちは追加しないでください。\n", encoding="utf-8",
        )
        documents = {
            "concept.md": "# 潮待ちの夏\n\n海辺の町で幼なじみが友情を取り戻す青春短編。\n",
            "world.md": "# 世界設定\n\n現代日本の海辺の町。超常現象は存在しない。\n",
            "characters.md": "# 人物設定\n\n凪は慎重な高校生。湊は率直な幼なじみ。\n",
            "plot.md": "# 全体構成\n\n壊れた祭りの看板を直す過程で二人が過去の口論を解きほぐす。\n",
            "style.md": "# 文体設定\n\n凪の三人称限定視点、過去時制。簡潔に描く。\n",
            "canon.md": "# 確定事実\n\n二人は口論以来疎遠で、祭りの看板は壊れている。\n",
        }
        for path in DEFAULT_CHAPTER_REVISION_CONTEXT:
            (root / path).write_text(documents[path], encoding="utf-8")
        (root / "chapters" / "0001.md").write_text(
            "# 第1章\n\n## 目的\n友情を回復する。\n\n## 開始状態\n二人は疎遠。\n\n"
            "## 終了状態\n二人は和解。\n\n## 主要な出来事\n看板を共同で修理する。\n\n"
            "## 収録話\n0001-0002\n\n## 接続条件\n一章完結。\n\n## 完成後のあらすじ\n未作成\n",
            encoding="utf-8",
        )
        (root / "episodes" / "0001.md").write_text(
            "## 話タイトル\n錆びた釘\n\n## 本文\n"
            "凪が倉庫へ入ると、湊は壊れた看板を抱えていた。二人は目を合わせず、古い釘を抜いた。"
            "板を押さえる手が触れ、凪は去年の口論を謝った。湊も、返事を待たず背を向けたことを認めた。\n",
            encoding="utf-8",
        )
        (root / "episodes" / "0002.md").write_text(
            "## 話タイトル\n新しい文字\n\n## 本文\n"
            "凪と湊は看板へ祭りの名を塗り直した。夕日で文字が乾くころ、湊が明日も一緒に来ようと言った。"
            "凪はうなずき、二人で修理した看板を入口へ掲げた。\n",
            encoding="utf-8",
        )
        request = select_request(root, load_state(root))
        interpretation = parse_request_interpretation(json.dumps({
            "kind": "continue", "summary": "第1章を評価し完成させる", "targets": [],
            "required_conditions": ["友情の回復", "看板の共同修理", "三人称限定視点"],
            "prohibited_changes": ["超常現象", "夢落ち"], "additional_material": [],
            "decision_answers": [], "ambiguities": [], "requested_units": 1,
            "requested_until": None,
        }, ensure_ascii=False), request.content)
        config = {
            "dotenv": {"files": [str(Path.home() / ".env")]},
            "providers": {"openrouter": {
                "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_APIKEY",
            }},
            "models": {"chapter-revision-integration": {
                "provider": "openrouter", "model": "deepseek/deepseek-v4-flash:nitro",
                "max_tokens": 4096, "parameters": {"temperature": 0},
            }},
            "roles": {role: ["chapter-revision-integration"] for role in ("reviewer", "reviser")},
            "limits": {
                "generation_calls": 3, "review_calls": 5, "revision_calls": 3,
                "retry_calls_per_request": 2,
            },
            "request": {"timeout_seconds": 120, "retry_attempts": 2},
        }
        result = produce_chapter_revision(
            root, request, interpretation, 1,
            LLMClient(config, load_environment(config)), all_chapters_complete=True,
        )
        if result.status != "completed" or result.best is None or result.completion_update is None:
            raise RuntimeError(f"chapter revision did not complete: {result.status} {result.reason}")
        if result.completion_update.next_phase != "final_revision":
            raise RuntimeError("chapter completion returned an unexpected next phase")
        print(
            "OpenRouter chapter revision passed: "
            f"model={result.calls[-1].completion.model_reference} "
            f"revisions={result.best.revision_count} calls={dict(result.call_counts)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
