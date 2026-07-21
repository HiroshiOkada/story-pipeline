# 状態と実行記録の仕様

## 1. 基本方針

`.story-pipeline/state.json` は作品全体の現在状態、`.story-pipeline/runs/NNNN.json` は要求 `NNNN` の実行状態を保持する。いずれも UTF-8、インデント 2、末尾改行ありの厳密な JSON とし、コメントと未知のキーを許可しない。

保存時は同じディレクトリの一時ファイルへ完全な JSON を書き、flush と同期を完了してから原子的に置換する。実行記録を先に、作品状態を後に保存する。どちらかの保存に失敗した場合は処理を続けず、既存の有効なファイルを破壊しない。

## 2. `state.json`

### 2.1 構造

```json
{
  "schema_version": 1,
  "phase": "concept",
  "next_chapter": 1,
  "next_episode": 1,
  "completed_chapters": [],
  "completed_episodes": [],
  "current_chapter": null,
  "pending_reviews": [],
  "pending_decisions": [],
  "last_request": null,
  "active_request": null,
  "updated_at": "2026-07-22T01:23:45Z"
}
```

| キー | 型 | 制約 |
| --- | --- | --- |
| `schema_version` | integer | `1` |
| `phase` | string | 定義済み phase |
| `next_chapter` | integer | `1..9999` |
| `next_episode` | integer | `1..9999` |
| `completed_chapters` | integer[] | 昇順、一意、各値 `1..9999` |
| `completed_episodes` | integer[] | 昇順、一意、各値 `1..9999` |
| `current_chapter` | integer/null | 未開始時は `null`、それ以外は `1..9999` |
| `pending_reviews` | object[] | 未完了の評価対象 |
| `pending_decisions` | object[] | 人間による判断待ち事項 |
| `last_request` | integer/null | 完了、失敗、確認待ちのいずれかで終了した最新要求 |
| `active_request` | integer/null | 実行中または再開対象の要求 |
| `updated_at` | string | UTC RFC 3339 |

phase は `concept`、`foundation`、`plotting`、`episode_planning`、`drafting`、`chapter_revision`、`final_revision`、`completed` のいずれかとする。

`pending_reviews` の要素は `{"target_type": "episode", "target_number": 1, "reason": "..."}` とし、`target_type` は `chapter`、`episode`、`novel` のいずれかとする。`novel` の `target_number` は `null` とする。

`pending_decisions` の要素は次の構造とする。

```json
{
  "id": "request-0003-decision-01",
  "request": 3,
  "question": "結末を変更してよいか",
  "reason": "要求が採用済みの結末と矛盾するため",
  "choices": ["既存の結末を維持", "新しい結末へ変更"],
  "created_at": "2026-07-22T01:23:45Z"
}
```

ID は要求内で一意とし、解決済み判断は配列から除く。解決内容は対応する要求、報告、実行記録に残す。

### 2.2 整合条件

- `next_chapter` と `next_episode` はそれぞれ完了済み最大番号より大きい。
- 完了済み番号には対応する章または本文ファイルが存在する。
- `completed` では未完了の review と decision がなく、`active_request` は `null` である。
- `active_request` が非 `null` なら同番号の実行記録が存在し、その状態は `running` または `failed` である。人間確認待ちの要求は終了済みとし、後続要求で回答するため active に残さない。
- `last_request` は存在する実行記録の最大番号以下である。要求は番号順に処理するため、通常は最大番号と一致する。

scaffold 時は `phase=concept`、次番号はともに `1`、配列は空、request と current chapter は `null` とする。

## 3. `runs/NNNN.json`

### 3.1 構造

```json
{
  "schema_version": 1,
  "request_number": 0,
  "status": "running",
  "started_at": "2026-07-22T01:23:45Z",
  "updated_at": "2026-07-22T01:23:45Z",
  "finished_at": null,
  "request_sha256": "...",
  "start_commit": "...",
  "end_commit": null,
  "current_step": "interpret_request",
  "steps": [],
  "call_counts": {
    "generation": 0,
    "review": 0,
    "revision": 0,
    "summary": 0
  },
  "model_attempts": [],
  "input_hashes": {},
  "output_hashes": {},
  "restored_files": [],
  "fallbacks": [],
  "errors": [],
  "resume": null
}
```

`request_sha256`、`input_hashes`、`output_hashes` はファイルの生バイト列に対する小文字 64 桁の SHA-256 とする。Git コミットは完全な object ID を保存する。

### 3.2 工程

`steps` の要素は次の形式とする。

```json
{
  "id": "interpret_request",
  "status": "completed",
  "started_at": "2026-07-22T01:23:45Z",
  "finished_at": "2026-07-22T01:23:48Z",
  "input_hashes": {"requests/0000.md": "..."},
  "output_hashes": {},
  "result": "構想作成要求として受理"
}
```

工程 status は `pending`、`running`、`completed`、`failed`、`skipped` のいずれかとする。工程 ID はパイプライン仕様で定義する固定 ID を使用する。同じ種類を繰り返す場合は `review_episode_0001_01` のように対象番号と 1 始まりの試行番号を付ける。

`result` は秘密情報を含まない短い機械可読または人間可読の要約とし、LLM 応答全文を保存しない。

### 3.3 モデル試行とエラー

`model_attempts` は論理呼び出しごとに role、モデル定義名、API 上のモデル名、開始・終了日時、結果分類、消費トークン数が取得できた場合の値を記録する。プロンプト、応答全文、API キー、認証ヘッダーは記録しない。

エラー要素は次の形式とする。

```json
{
  "step": "review_episode_0001_01",
  "category": "rate_limit",
  "message": "再試行上限に到達しました",
  "retryable": true,
  "occurred_at": "2026-07-22T01:23:45Z"
}
```

`message` はサニタイズ済みとし、リクエストヘッダー、環境変数値、レスポンス本文をそのまま含めない。

## 4. 要求状態遷移

```text
pending -> running -> completed
                   -> failed
                   -> awaiting_human
failed ----------- -> running
awaiting_human -> pending（後続の回答要求）
```

- `pending` は未処理要求がファイルとして存在し、実行記録がまだない論理状態であり、JSON には保存しない。
- 開始時コミット成功後、実行記録を `running` で作成し、`state.active_request` を設定する。
- 全工程と終了時コミットが成功した後に `completed` とする。
- 自動再試行で解決できず、同じ要求から安全に再開可能なら `failed` とする。
- 人間の選択や明示的確認が必要なら `awaiting_human` とする。
- `completed` と `awaiting_human` では `state.active_request` を `null` にする。`failed` は同じ要求の再開位置として active に残す。
- 終了状態では `finished_at` を設定する。`failed` の再開時は status を `running`、`finished_at` を `null` に戻すが、最初の `started_at` は保持する。`awaiting_human` の実行記録は再開せず、回答を記した後続要求の新しい実行記録を作る。

終了時コミットの object ID はコミット作成後にしか得られないため、`end_commit` はその終了時コミット自身を指さない。初期実装では `null` のままとし、コミット成功を Git 履歴と報告で確認する。将来、追跡コミットを導入するまで自己参照値を推測してはならない。

## 5. 再開規則

### 5.1 再開可否の判定

`run` は未処理要求の探索前に `state.active_request` を確認する。非 `null` の場合は次をすべて満たすときだけ再開する。

1. 対応する要求、実行記録、開始時コミットが存在する。
2. 要求ファイルのハッシュが `request_sha256` と一致する。
3. 完了済み工程の出力ファイルが存在し、記録済みハッシュと一致する。
4. 現在の設定が読み込み可能である。
5. 自動作成済みの後続要求が空またはテンプレートのままであり、再開対象とは別の新規要求が混入していない。

1 つでも満たさなければ自動変更せず、`validate` の実行と人間による解決を案内する。

`pending_decisions` がある場合は active request の再開ではなく、最若番号の未処理要求を回答要求として解釈する。必要な判断 ID への回答が含まれなければ `8` で停止し、その要求をコミットまたは処理しない。

### 5.2 再開位置

`resume` は `{"step": "draft_episode_0004", "reason": "api_timeout"}` の形式とする。再開時は完了済み工程をスキップし、最初の未完了工程から行う。ただし、工程入力のハッシュが変わった場合は、その入力に依存する工程を未完了へ戻す。採用済み作品ファイルは再生成しない。

LLM 呼び出し回数は要求単位の累積値を保持し、プロセス再起動でリセットしない。通信試行の一時的な回数だけは論理呼び出しの再開時にリセットできる。

### 5.3 プロセス中断

`running` の実行記録があり `run.lock` がない場合は中断とみなす。最後の `running` 工程を `failed` に更新し、中断エラーと再開位置を保存してから通常の再開判定へ進む。途中生成した一時ファイルは採用せず削除する。ハッシュが記録された確定済み出力だけを完了済みとみなす。

## 6. 状態と実ファイルの照合

`status` と `validate` は少なくとも次を検査する。

- JSON スキーマと列挙値
- 要求、報告、実行記録の番号対応
- active/last request と実行 status
- 完了章、完了話とファイルの存在
- 次番号と既存最大番号
- 実行記録に保存された入出力ハッシュ
- phase に必要な成果物の存在

`status` は不一致を警告として表示し、変更しない。`validate` は不一致をエラーまたは警告に分類して非ゼロ終了できるが、自動修復しない。
