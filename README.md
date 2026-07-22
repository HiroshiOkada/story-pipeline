# Story Pipeline

Story Pipeline は、人間が Markdown で与えた要求を起点に、LLM と協調して小説の構想、設定、構成、話計画、本文、改稿を段階的に制作する CLI です。成果物、状態、実行記録をファイルと Git コミットに残し、要求ごとに人間が方針を修正できます。

初めて利用する方、設定項目や停止時の対処まで確認したい方は、[Story Pipeline 取扱説明書](docs/user-manual.md)を参照してください。

## 動作要件

- Python 3.12 以上
- Git
- OpenAI Chat Completions 互換 API の認証情報

## 導入

PyPI 公開前は、リポジトリのチェックアウトから導入します。

```console
git clone <repository-url>
cd story-pipeline
python -m pip install .
story-pipeline --version
```

開発時は [uv](https://docs.astral.sh/uv/) を使ってコマンドを実行できます。

```console
uv run story-pipeline --help
uv run python -m unittest discover -s tests -p 'test_*.py'
```

## クイックスタート

1. 空のディレクトリを初期化します。

   ```console
   mkdir my-story
   story-pipeline init my-story
   cd my-story
   ```

2. `story-pipeline-config.jsonc` の provider、model々の role を利用する API に合わせて設定します。初期値は OpenAI と `gpt-4.1` です。
3. 設定の `api_key_env` が示す環境変数を、`~/.env`、作品ルートの `.env`、またはプロセス環境に設定します。

   ```dotenv
   OPENAI_API_KEY=your-api-key
   ```

4. `requests/0000.md` に作りたい作品と条件を書き、検査後に実行します。

   ```console
   story-pipeline validate
   story-pipeline status
   story-pipeline run
   ```

5. 処理後の `requests/0000_agent.md` と作品ファイルを確認し、次に作成された `requests/0001.md` へ追加要求や判断を記入します。

## 設定

`story-pipeline-config.jsonc` はコメント付き JSON です。主な設定は次のとおりです。

- `dotenv.files`: 認証情報を読み込む dotenv ファイル。プロセス環境を最優先とし、先に読み込んだ値を保持します。
- `providers`: API の `base_url` と API キー環境変数名。
- `models`: provider、モデル識別子、任意の `max_tokens` と追加パラメーター。
- `roles`: `planner`, `writer`, `reviewer`, `reviser`, `summarizer` ごとのモデル候補。前から順に試行します。
- `limits`: 1要求内の生成・検査・改稿・要約回数と変更行数の上限。
- `request`: HTTP タイムアウトと通信再試行回数。

日常の操作方法は[取扱説明書](docs/user-manual.md)、設定契約とファイル形式の厳密な詳細は [`docs/detailed-specification`](docs/detailed-specification/README.md) を参照してください。

## コマンド

| コマンド | 動作 |
| --- | --- |
| `story-pipeline init [PATH]` | 空のディレクトリに作品プロジェクトと検証済み初期 commit を作成します。 |
| `story-pipeline status` | 現在のフェーズと次の標準処理を副作用なしで表示します。 |
| `story-pipeline validate` | 設定、状態、成果物、Git の整合性を API 呼び出しなしで検査します。 |
| `story-pipeline migrate-state` | 章・話対応表と既存本文を検証し、旧実装の誤った制作状態を明示的に移行します。 |
| `story-pipeline run` | 最若番号の未処理要求を1件処理します。 |

## 安全な運用

- `run` は Git 作業ツリー、実行ロック、設定、認証情報、API 接続を確認してから作品を変更します。
- `init` は scaffold の既知4ファイルだけを `Initialize story project` として commit し、作業ツリーを clean にします。
- 人間が編集した要求と設定を開始時コミットに、採用済み成果物と実行記録を終了時コミットに保存します。
- staged 変更、競合、Git の履歴操作中、想定外の変更がある場合は自動処理を開始しません。
- `.env` と `.story-pipeline/run.lock` は初期化時から Git の除外対象です。API キーを設定、報告、コミットへ記録しないでください。
- 終了ステータスが `awaiting_human` の場合は、報告の判断 ID を確認して次の要求に回答します。
- 終了ステータスが `failed` の場合は、空の次要求を残したまま同じ要求を再実行できます。失敗原因に合わせて元の要求を直した場合、その内容は改訂履歴と入力 commit に記録されます。次要求へ具体的な内容を書いた場合は、改訂か新規要求かを決めるまで安全停止します。
- 本文評価後に knowledge 更新だけが失敗した場合、検証済み本文は内部 checkpoint に保存され、次回は本文を再生成せず knowledge 工程から再開します。checkpoint や作品ファイルを手動変更した場合は `validate` で確認してください。
- `run` は LLM 通信の開始、再試行待機、フォールバック、30秒ごとの heartbeat を JSON Lines で標準エラーへ出力します。prompt、応答本文、API キー、完全 URL は出力しません。
- `.story-pipeline/runs/NNNN.json` に論理呼び出し、transport 試行、待機時間、provider usage、lifecycle、incident ID を記録します。usage が提供されない値は推測せず `null` / `unknown` のまま扱います。予期しない失敗時は標準エラーの incident ID を実行記録と照合できます。
- 旧バージョンで章内最終話の後も次話計画を指している場合は、作業ツリーを整理して `story-pipeline migrate-state` を実行します。作品本文は変更せず、状態変更だけを専用 commit に保存します。

## 開発者向け検証

```console
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall -q story_pipeline tests
uv build
uv run python tests/integration_distribution.py dist/story_pipeline-0.1.0-py3-none-any.whl
git diff --check
```

OpenRouter を使う統合テストは、`~/.env` の `OPENROUTER_APIKEY` と `deepseek/deepseek-v4-flash:nitro` を使用します。外部 API を使うため、通常の単体テストからは分離しています。
