# CLI 詳細仕様

## 1. 共通インターフェース

```text
story-pipeline [-h] [--version]
story-pipeline init [PATH]
story-pipeline run
story-pipeline status
story-pipeline validate
story-pipeline migrate-state
```

- 引数なし、`-h`、`--help` はヘルプを標準出力へ表示し、終了コード `0` で終了する。
- `--version` は `story-pipeline <version>` の 1 行を表示する。
- 未知のコマンド・オプション、余分な位置引数は使用方法を標準エラーへ表示する。
- 初期実装は対話入力、暗黙の確認、色付き出力を使用しない。
- 通常結果は標準出力、警告とエラーは標準エラーへ出す。
- API キー、認証ヘッダー、完全な API 応答を表示してはならない。

## 2. 終了コード

| コード | 意味 |
| --- | --- |
| `0` | 正常終了。`status` で警告だけがある場合を含む |
| `2` | CLI の使用方法が不正 |
| `3` | 未初期化または作品ルートを特定できない |
| `4` | 設定、状態、作品ファイルの検証エラー |
| `5` | 作業ツリーまたは Git 操作のエラー |
| `6` | ロック競合 |
| `7` | API、モデル、応答形式の処理失敗 |
| `8` | 人間の判断・確認が必要 |
| `9` | ファイル I/O その他の内部処理失敗 |

複数の問題がある場合は、処理を安全に開始できない最初の問題のコードを返す。`validate` は全検査を可能な範囲で続行し、エラーがあれば `4` を返す。

## 3. `init`

### 3.1 入力

`PATH` は省略時 `.` とする。`-`、複数パス、ファイルへのパスを受け付けない。

### 3.2 処理

1. 対象パスを検証する。
2. 初期化済み、空、`.git` だけ、その他の空でない状態に分類する。
3. 既存 Git repository であれば、作品ルートと Git ルートの一致、進行中操作の不在、worktree と index に差分がないことを副作用なしで検査する。
4. 作成予定パスを検査し、scaffold を作成する。
5. Git repository でなければ `git init` する。
6. `.gitignore`、`.story-pipeline/state.json`、`requests/0000.md`、`story-pipeline-config.jsonc` だけを stage し、予定集合と index の完全一致を確認して `Initialize story project` commit を作成する。
7. HEAD が存在し worktree が clean な状態で、作品ルートと次の操作を表示する。

成功時の最小出力は次の形式とする。

```text
Initialized Story Pipeline project: /absolute/path
Next request: requests/0000.md
Run: story-pipeline run
```

既に初期化済みなら変更せず、設定ファイルのパスを示して `4` で終了する。空でない未初期化ディレクトリも変更せず、空のディレクトリを指定するよう案内して `4` とする。既存 Git 差分は一切変更せず `5` で停止する。Git identity 不足または commit 失敗時は、今回 stage した scaffold を index から外し、作成済みファイルを保持したまま `5` で停止して identity 設定と手動 commit を案内する。

### 3.3 副作用

scaffold と、必要な場合の `.git`、初期 commit を作成する。API 接続と要求処理は行わない。

## 4. `run`

### 4.1 前提検査

次の順序を守る。

1. 作品ルートを特定する。
2. 設定、状態、Git リポジトリを読み取り検証する。
3. ロックを獲得する。
4. 作業ツリーを分類する。
5. 再開対象、または最若番号の未処理要求を決定する。
6. 要求内容が空でないことを確認する。
7. 必要な provider の認証情報と初期接続を確認する。
8. 管理ファイルの直接変更を復元する。
9. 人間入力を開始時コミットへ保存する。
10. 実行記録を作成または再開する。

接続確認までは作品ファイルを変更しない。管理ファイルの復元は接続確認成功後、開始時コミット前にだけ行う。

未処理要求がない場合は `No pending request.` と表示して `0` で終了する。後続要求があるが先行要求の確認待ちを解決しない場合は、必要な判断 ID を示して `8` とする。

### 4.2 本処理と終了処理

本処理は `pipeline.md` の工程を実行する。終了状態にかかわらず、書き込み可能なら次を行う。

1. 実行記録を確定する。
2. 作品状態を確定する。
3. 処理報告を作成する。
4. CLI が作成・変更したファイルを終了時コミットへ保存する。
5. 終了 status にかかわらず、次の要求テンプレートを未コミットで作成する。
6. ロックを解放する。

`failed` または `awaiting_human` でも、報告と再開情報を終了時コミットして次要求を作成する。`failed` を同じ要求から再開する間、次要求は空のテンプレートとして残す。失敗原因を解消するため active 要求を直した場合は、変更を新しい要求 revision として開始時 commit と run に記録する。人間が先に次要求を書き始めた場合は順序を曖昧にしないため再開せず、active 要求の改訂か新規要求かを決めるよう案内する。`awaiting_human` では次要求を回答要求として処理する。

成功時は要求番号、status、変更ファイル、使用した各 role のモデル、呼び出し回数、報告パス、次要求パスを表示する。失敗時は秘密を除いた原因、報告パス、再開方法を標準エラーへ表示する。

### 4.3 ロック解放

正常終了、捕捉可能な例外、割り込みでは finally 相当の処理で解放する。プロセス強制終了で残ったロックは自動削除せず、Git 安全仕様の stale lock 判定に従う。

## 5. `status`

### 5.1 表示項目

```text
Root: /absolute/path
Phase: drafting
Last request: 0003 (completed)
Active request: none
Current chapter: 0001
Next episode: 0004
Completed chapters: 0
Completed episodes: 3
Pending reviews: 0
Pending decisions: 0
Next action: create episode plan 0004
```

実際のファイルとの軽量な照合を行い、不一致は `Warning: ...` として標準エラーへ表示する。JSON 全件のハッシュ検証や API 接続は行わない。

### 5.2 副作用

ファイル、Git、ロック、API に変更を加えない。ロックが存在する場合は active process 情報を表示するだけとする。

## 6. `validate`

### 6.1 検査範囲

- 設定 JSONC と全参照
- state と全 run JSON のスキーマ
- 状態、要求、報告、成果物の対応
- 採番と phase の整合性
- 記録されたファイルハッシュ
- 管理対象パスの種類と作品ルート外リンク
- Git リポジトリ、追跡状態、現在の変更分類
- `.gitignore` の必須除外
- API キー環境変数の存在（値は表示しない）

既定ではネットワーク接続や LLM 呼び出しを行わない。

### 6.2 出力

各問題を次の形式で 1 行にする。

```text
ERROR STATE_COMPLETED_FILE_MISSING completed episode has no file: episodes/0003.md
WARNING UNTRACKED_UNKNOWN_FILE file is not managed by Story Pipeline: notes.txt
```

問題がなければ `Validation passed.`、警告だけなら件数を表示して `0`、エラーがあればエラー・警告件数を表示して `4` とする。自動修復、Git add、復元、フォーマット変更は行わない。

## 7. `migrate-state`

`migrate-state` は、旧実装が本文採用後も `episode_planning` を指している既存状態を、章・話対応表から明示的に修正する。API は呼び出さない。

次をすべて満たす場合だけ実行する。

1. active request と実行ロックがない。
2. Git repository が通常状態で、tracked、staged、未知の untracked 差分がない。自動作成された実質空の次要求だけは残せる。
3. 章番号と全収録話が1始まりで連続し、重複・欠落・逆順がない。
4. `completed_episodes` が実在する本文の連続 prefix と完全一致する。
5. `completed_chapters` が章対応表の連続 prefix で、各完了章の全本文が存在する。

条件を満たす場合、phase、current/next chapter、next episode だけを再計算し、`.story-pipeline/state.json` を `Migrate story state` commit に保存する。作品ファイル、要求、run、既存 commit は変更しない。すでに正規状態なら変更も commit も作らない。

## 8. エラー表示

捕捉済みエラーは原則として次の3行以内にする。

```text
Error: <利用者が理解できる原因>
Location: <設定キーまたは作品ルート相対パス>
Action: <安全な次の操作>
```

デバッグ用 traceback は初期実装の通常表示に含めない。予期しない例外も秘密情報を除去した一般エラーとして `9` を返す。
