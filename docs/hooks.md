# Hook 実装仕様

## 全体マップ

| hook | タイミング | 役割 |
|------|-----------|------|
| PreToolUse | Edit/Write 実行直前 | ファイル特化 findings 注入 |
| SessionStart | セッション開始時 | high/critical pending 件数を通知（既存を改良） |
| SessionEnd | セッション終了時 | dismissed パターンを CLAUDE.md に追記 |
| PostToolUse | Edit/Write 完了後 | 編集カウントを +1（バッチトリガー判定用） |

PostToolUse で毎回 /ifr を実行しない。カウントのみ増やし、5回ごとにバッチレビューを提案するだけ。

---

## 1. PreToolUse: pre-tool-inject-findings.py

### 処理フロー

```
stdin → JSON (tool_name, tool_input) 受信
    │
    ├── tool_name が Edit/Write/MultiEdit でなければ → exit 0（スルー）
    │
    ├── file_path を tool_input から抽出
    │       Edit    → tool_input["file_path"]
    │       Write   → tool_input["file_path"]
    │       MultiEdit → tool_input["edits"] から全ファイルを重複除去して収集
    │
    ├── Phase A: ファイル特化クエリ（SNR フィルタ: critical/high/warning・30日・NOT EXISTS）
    │       1件以上 → 注入文を stdout に出力 → Claude のコンテキストに追加
    │       0件     → Phase B へ
    │
    └── Phase B: プロジェクト横断フォールバック（severity=critical のみ・LIMIT 5）
            1件以上 → "PROJECT-WIDE CRITICAL PATTERNS" として注入
            0件     → exit 0（何も注入しない）
```

### 実装（hooks/pre-tool-inject-findings.py）

現行実装を参照: `hooks/pre-tool-inject-findings.py`

主な実装ポイント:
- **Phase A パス正規化**: `replace(file_path, '\\', '/') = ?` でバックスラッシュ不一致を防止
- **Phase A repo_root スコープ**: `replace(COALESCE(repo_root, ''), '\\', '/') = ?` で他プロジェクトの findings を除外
- **Phase A 旧データフォールバック**: `OR repo_root IS NULL` で Phase 4 以前の INSERT（repo_root 未設定）にもマッチ
- **NOT EXISTS 正規化**: サブクエリ内の `f2.file_path = findings.file_path` も両辺 `replace()` 適用。`f2.resolution IN ('accepted', 'fixed')` で解決済みパターンの再注入を防止
- **MultiEdit 全 edits 対応**: 先頭ファイルのみではなく全 edits から重複除去して複数ファイルを対象にする
- **session_id 必須**: 空文字列の場合は注入をスキップ（SNR 破壊防止）
- **セッション内 dedup**: `~/.claude/inject-state/{session_id}.txt` に注入済み ID を append-only で管理
- **Phase B depth check**: git root が 4 セグメント未満（ドライブルート等）の場合はスキップ
- **Phase B repo_root 再利用**: Phase A で取得した `repo_root` を Phase B でも使い回す（`_get_project_root` の二重サブプロセス呼び出しを回避）
- **cutoff フォーマット**: `strftime('%Y-%m-%dT%H:%M:%S')` で DB の秒精度 created_at と統一
- **鮮度 OR 条件**: `(created_at >= cutoff OR COALESCE(last_relevant_edit, '2000-01-01') >= relevance_cutoff)` で、30日超でも 14日以内に関連ファイルが編集された finding を注入対象に復活
- **dismiss ディスカバラビリティ**: finding ID をインライン表示（`【severity】#ID category: summary`）+ dismiss コマンドワンライナーを注入テキストに追加
- **FP パターン注入**: `get_fp_patterns()` でユーザー 2 回以上 dismiss 承認パターンを取得し、注入ブロック末尾に `--- 学習済みパターン ---` セクションとして追加（最初のファイルのみ、重複防止）
- **Phase B 鮮度 OR 条件**: Phase A と同様に `last_relevant_edit` 鮮度条件を Phase B にも適用（critical は見逃すと致命的なため）
- **cwd フォールバック**: `_get_project_root(file_path, cwd)` で新規ファイル（親ディレクトリ未存在）時に payload の cwd へフォールバック
- **UNC パス先頭保持クリーンアップ**: `_get_project_root()` で UNC パスの先頭 `//` を保持しつつ内部の `//` を除去（`//server/share` を壊さない）
- **dedup ローテーション**: `_load_injected_ids()` で `DEDUP_ROTATION_LIMIT`（2000行）超過時に古い半分を削除
- **DB 接続統合**: `main()` で 1 回だけ `sqlite3.connect()` し、`get_findings()` と `get_fp_patterns()` で共有（二重接続排除）
- **NOT EXISTS repo_root スコープ**: `f2.repo_root IS NULL AND findings.repo_root IS NULL` で旧データの NULL 同士のみマッチさせる（クロスリポジトリ誤除外防止）
- **fp_reason サニタイズ**: 注入テキスト内の fp_reason を改行除去 + 80文字制限（コンテキスト圧迫防止）
- **file_paths 収集時正規化**: バックスラッシュをスラッシュに変換してから DB 照合（Windows パス不一致防止）
- **共通 config モジュール**: `hooks/config.py` で DB_PATH・STATE_DIR・COUNTER_DIR を一元管理（3ファイル間の重複排除）

### settings.json 登録

#### Phase 1 用（まず PreToolUse のみ登録する）

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python C:\\Users\\Tenormusica\\claude-review-pdca\\hooks\\pre-tool-inject-findings.py"
          }
        ]
      }
    ]
  }
}
```

> **注意**: 既存 hook（claude-subconscious の `pretool_sync.ts` 等）が存在する場合は
> `PreToolUse` 配列に **追記** して共存させること（上書き禁止）。

#### Phase 2 以降の完全版（PostToolUse / SessionEnd を追加）

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python C:\\Users\\Tenormusica\\claude-review-pdca\\hooks\\pre-tool-inject-findings.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python C:\\Users\\Tenormusica\\claude-review-pdca\\hooks\\post-tool-edit-counter.py"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python C:\\Users\\Tenormusica\\claude-review-pdca\\hooks\\session-end-learn.py"
          }
        ]
      }
    ]
  }
}
```

> **注意**: SessionStart の pending 通知強化は既存の `review-feedback.py` スクリプトを差し替えるため、
> 既存 SessionStart hook の設定を直接変更する（上記 JSON には含めていない）。
> 既存 hook が存在する場合は各イベントの配列に追記して共存させること。

---

## Codex / 手動運用での代替コマンド

Claude Code では hook で自動発火させるが、Codex では同じ処理を**明示コマンド**で代替する。

```bash
python scripts/prepare-implementation-context.py \
  --session-id codex-sess-1 \
  --cwd C:/path/to/repo \
  --prompt "sc-rfl この file を修正" \
  --file-path src/app/main.py
```

このコマンドは:

1. prompt 内の implementation marker（`sc-rfl`, `/rfl` など）を検出
2. `~/.claude/hooks/implementation-session.json` を更新
3. `pre-tool-inject-findings.py` に hook payload 相当の JSON を渡す
4. 同じ注入テキストを stdout に返す

つまり **hook を持たないランタイムでも、PreToolUse 相当を同じロジックで再利用できる**。

---

## 2. SessionStart: 強化版 pending 通知

現状: 全件カウントのみ通知
改善: critical/high の件数を分けて表示 + 全件はリンクのみ

```python
conn.row_factory = sqlite3.Row
try:
    rows = conn.execute("""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical,
          SUM(CASE WHEN severity = 'high'     THEN 1 ELSE 0 END) AS high
        FROM findings
        WHERE dismissed = 0 AND resolution = 'pending'
    """).fetchone()
except sqlite3.OperationalError:
    rows = None

if rows and (rows['critical'] or 0) > 0:
    print(f"🚨 CRITICAL findings: {rows['critical']} 件")
if rows and (rows['high'] or 0) > 0:
    print(f"⚠️ HIGH findings: {rows['high']} 件")
if rows and (rows['total'] or 0) > 0:
    print(f"（全 {rows['total']} 件: python review-feedback.py query --resolution pending）")
```

---

## 3. PostToolUse: 編集カウント管理

毎回 /ifr を呼ばない。セッション別カウントファイルで管理し、5回ごとに提案する。

現行実装を参照: `hooks/post-tool-edit-counter.py`

主な実装ポイント:
- **セッション別ファイル**: `~/.claude/edit-counter/{session_id}.txt`（グローバル単一ファイルから変更）
- **イベント記録形式**: `"e\n"` を append（`"\n"` だと `splitlines()` が空文字列を返しカウントが常時 0 になる）
- **カウント方法**: `sum(1 for line in lines if line)`（非空行のみ）
- **ローテーション**: `ROTATION_LIMIT = 5000` 行超で `write_text("")` ゼロバイトリセット（`"\n" * N` 書き込みは偽通知バグを起こすため禁止）
- **通知条件**: `count > 0 and count % BATCH_THRESHOLD == 0`（`count=0` での偽通知を防止）
- **last_relevant_edit 更新**: 編集ファイルに紐づく pending findings の `last_relevant_edit` を現在時刻に更新。PreToolUse の OR 鮮度条件と連動し、古い finding でも最近編集されたファイルなら注入復活する

---

## 4. SessionEnd: dismissed 学習 → CLAUDE.md 追記

現行実装を参照: `hooks/session-end-learn.py`

主な実装ポイント:
- **CLAUDE.md 探索**: `_find_claude_md()` が `tuple[Path | None, str | None]` を返す（CLAUDE.md パス + repo_root）
  - 探索順: `payload["cwd"]` → `git rev-parse --show-toplevel` → `Path.cwd()` の 3 段フォールバック
- **repo_root スコープ付き学習クエリ**: repo_root が取得できた場合は `replace(COALESCE(repo_root, ''), '\\', '/') = ?` で同一リポジトリの findings のみ学習対象にする。取得できない場合はフィルタなし（git 管理外プロジェクト用フォールバック）
- **severity ガード**: `severity != 'critical'` で critical を学習対象から除外（false positive パターンとして学習すべきでない）
- **ホームディレクトリ保護**: 全候補（cwd・git root・Path.cwd()）で `~/CLAUDE.md` への書き込みを防ぐ共通チェック
- **resolution フィルタ**: `AND resolution = 'pending'` で stale 化した findings を学習対象から除外
- **マーカー分離**: `<!-- auto-generated:fp-patterns -->` ～ `<!-- end-auto-generated:fp-patterns -->` で自動生成部分を囲み、ユーザー手動追記を保護。旧形式（マーカーなし）からの自動移行時も、自動生成エントリ（`- [` で始まる行）以外のユーザー追記を保護して auto_block の前に挿入する
- **fp_reason サニタイズ**: `_sanitize_fp_reason()` で改行→スペース変換 + 先頭 `#` の全角変換 + 80文字制限（CLAUDE.md 構造破損防止）
- **UNC パス先頭保持**: `_find_claude_md()` 内の git パス正規化で UNC 先頭 `//` を保持
- **アトミック書き込み**: `tempfile.NamedTemporaryFile` → `os.replace()` で lost update を防止
- **クリーンアップエラーログ**: `_cleanup_inject_state()` の例外を `except Exception` で捕捉し stderr に出力（非致命的エラーを可視化）
- **クリーンアップ対象**:
  - `~/.claude/inject-state/` 配下の 24 時間以上古いセッション dedup ファイル（`.txt` / `.json`）
  - `~/.claude/edit-counter/` 配下の 24 時間以上古いセッション別カウントファイル（`{session_id}.txt` + `{session_id}_files.txt`）
  - `~/.claude/edit-counter.txt`（旧グローバル形式）の 10000 行超ローテーション（後方互換）
