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

- `completed_chapters` と `completed_episodes` は章・話対応表の連続した prefix である。
- `episode_planning` と `drafting` の `current_chapter` / `next_chapter` は最初の未完了章、`next_episode` はその章の最初の未完了話を指す。
- 現在章の全話が完了したら `phase=chapter_revision` とし、`current_chapter` を維持する。
- 章改稿完了後、未完了章があればその章へ進み、全章完了なら `phase=final_revision`、`current_chapter=null` とする。
- 最終番号9999以外では、次の予定対象がない場合の sentinel は作品内最大番号+1とする。9999は終端 phase でだけ同値 sentinel として許可する。
- 完了済み番号には対応する章または本文ファイルが存在する。
- `completed` では未完了の review と decision がなく、`active_request` は `null` である。
- `active_request` が非 `null` なら同番号の実行記録が存在し、その状態は `running` または `failed` である。人間確認待ちの要求は終了済みとし、後続要求で回答するため active に残さない。
- `last_request` は存在する実行記録の最大番号以下である。要求は番号順に処理するため、通常は最大番号と一致する。

scaffold 時は `phase=concept`、次番号はともに `1`、配列は空、request と current chapter は `null` とする。

### 2.3 章・話対応表

全 `chapters/NNNN.md` の `## 収録話` を章番号順に解析する。`0001` の単独番号と `0001-0003`、`0001〜0003` の範囲を許可する。章番号、章内話番号、章間話番号はすべて1始まり、昇順、一意、連続でなければならない。重複、欠落、逆順、範囲の逆転は該当する `chapters/NNNN.md ## 収録話` を location として拒否する。

## 3. `runs/NNNN.json`

### 3.1 構造

```json
{
  "schema_version": 3,
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
    "knowledge": 0,
    "summary": 0
  },
  "model_attempts": [],
  "input_hashes": {},
  "output_hashes": {},
  "restored_files": [],
  "fallbacks": [],
  "errors": [],
  "resume": null,
  "request_revisions": [
    {
      "sha256": "...",
      "input_commit": "...",
      "accepted_at": "2026-07-22T01:23:45Z",
      "applies_from_step": "interpret_request"
    }
  ],
  "resume_count": 0,
  "model_calls": [],
  "events": [],
  "incidents": [],
  "lifecycle": {
    "state": "executing",
    "history": []
  },
  "metrics": {
    "logical_calls": 0,
    "transport_attempts": 0,
    "retry_wait_ms": 0,
    "elapsed_ms": 0,
    "usage": {
      "prompt_tokens": null,
      "completion_tokens": null,
      "total_tokens": null,
      "cached_tokens": null,
      "reasoning_tokens": null
    }
  }
}
```

`request_sha256`、`input_hashes`、`output_hashes` はファイルの生バイト列に対する小文字 64 桁の SHA-256 とする。Git コミットは完全な object ID を保存する。`request_sha256` は現在受け入れている要求を表し、`request_revisions` は初回から最新版までの hash、入力 commit、受入時刻、再実行を始める工程を順に保存する。`start_commit` と `started_at` は初回値から変更しない。`resume_count` はプロセス単位の再開ごとに増やす。

version 1 の run は初回 revision を補い、version 2 は観測値を未取得として、どちらも version 3 へ互換読み込みする。旧記録の token や所要時間を 0 や推測値で補完しない。

### 3.2 本文 checkpoint

採用可能な本文を正式採用前に `.story-pipeline/checkpoints/NNNN/draft.json` へ保存する。checkpoint は要求番号と revision、対象話、全入力 hash、正規化済み本文と hash、機械検査、採用評価と評価対象 hash、生成・評価モデル、knowledge 状態、採用状態と期待出力 hash を持つ。プロンプト、応答全文、秘密は保存しない。

状態は次の順にだけ進める。

```text
knowledge=pending, adoption=pending
knowledge=completed, adoption=pending
knowledge=completed, adoption=ready
knowledge=completed, adoption=adopted
```

`ready` は本文、canon、characters の期待 hash を checkpoint へ先に保存した状態である。実ファイルの一致が0件なら未適用、全件なら全適用として再開できる。一部だけ一致する場合は部分適用として停止し、自動上書きしない。`adopted` は全出力 hash の一致後だけ記録する。

### 3.3 工程

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

### 3.4 モデル試行とエラー

`model_attempts` は後方互換用の論理呼び出し集計とする。version 3 の `model_calls` は各論理呼び出しに role、purpose、モデル、要求 revision、resume count、開始・終了日時、所要時間、fallback、truncation、usage を関連付ける。`transport_attempts` は各 HTTP 試行の試行番号と上限、所要時間、失敗分類、実待機時間を保存する。`metrics` は詳細値から再計算し、読み込み時に一致を検証する。

provider が usage を返した場合だけ prompt、completion、total、cached、reasoning token を保存する。提供されない値は `null` のままとし、0 と区別する。プロンプト、応答全文、API キー、認証ヘッダー、完全 URL は記録しない。

`lifecycle` は `starting`、`executing`、`finalizing`、`committed` の履歴を持つ。予期しない例外は例外本文や traceback を保存せず、`incidents` に UUID 形式の ID、component、例外クラス、現在工程、lifecycle、再試行可否だけを保存する。workflow、transport、finalizing、Git、lock、SIGINT/SIGTERM は別 component または例外クラスで識別する。

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
2. 要求ファイルのハッシュが `request_sha256` と一致するか、安全な要求改訂として開始時入力 commit に保存できる。
3. 完了済み工程の出力ファイルが存在し、記録済みハッシュと一致する。
4. 現在の設定が読み込み可能である。
5. 自動作成済みの後続要求が空またはテンプレートのままであり、再開対象とは別の新規要求が混入していない。

要求が変わっている場合は、未コミットなら要求と許可された設定だけを開始時入力 commit に保存し、commit 済みなら現在の HEAD を入力境界とする。新しい revision を追加して `request_sha256` を更新し、`interpret_request` から依存工程を再実行する。自動作成された後続要求が実質空でない場合は改訂か新規要求かが曖昧なため停止し、active 要求へ内容を移すか active run の扱いを決めるよう案内する。その他の条件を満たさなければ自動変更せず、`validate` の実行と人間による解決を案内する。

`pending_decisions` がある場合は active request の再開ではなく、最若番号の未処理要求を回答要求として解釈する。必要な判断 ID への回答が含まれなければ `8` で停止し、その要求をコミットまたは処理しない。

### 5.2 再開位置

`resume` は `{"step": "draft_episode_0004", "reason": "api_timeout"}` の形式とする。再開時は完了済み工程をスキップし、最初の未完了工程から行う。ただし、工程入力のハッシュが変わった場合は、その入力に依存する工程を未完了へ戻す。採用済み作品ファイルは再生成しない。

LLM 呼び出し回数は要求単位の累積値を保持し、プロセス再起動でリセットしない。各モデル試行は要求 revision 番号と対応付ける。通信試行の一時的な回数だけは論理呼び出しの再開時にリセットできる。

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
- 本文 checkpoint の schema、入力・候補・knowledge・期待出力 hash、部分適用状態
- phase に必要な成果物の存在

`status` は不一致を警告として表示し、変更しない。`validate` は不一致をエラーまたは警告に分類して非ゼロ終了できるが、自動修復しない。
