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
- **NOT EXISTS 正規化**: サブクエリ内の `f2.file_path = findings.file_path` も両辺 `replace()` 適用
- **MultiEdit 全 edits 対応**: 先頭ファイルのみではなく全 edits から重複除去して複数ファイルを対象にする
- **session_id 必須**: 空文字列の場合は注入をスキップ（SNR 破壊防止）
- **セッション内 dedup**: `~/.claude/inject-state/{session_id}.txt` に注入済み ID を append-only で管理
- **Phase B depth check**: git root が 4 セグメント未満（ドライブルート等）の場合はスキップ
- **cutoff フォーマット**: `strftime('%Y-%m-%dT%H:%M:%S')` で DB の秒精度 created_at と統一

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

---

## 4. SessionEnd: dismissed 学習 → CLAUDE.md 追記

現行実装を参照: `hooks/session-end-learn.py`

主な実装ポイント:
- **CLAUDE.md 探索**: `payload["cwd"]` → `git rev-parse --show-toplevel` → `Path.cwd()` の 3 段フォールバック
- **ホームディレクトリ保護**: `cwd` がホームディレクトリの場合に `~/CLAUDE.md` を掴むのを防ぐチェックを追加
- **ブロックヘッダー**: `## 学習済み false positive パターン（自動生成）`（日付ラベルなし）
- **アトミック書き込み**: `tempfile.NamedTemporaryFile` → `os.replace()` で lost update を防止
- **クリーンアップ対象**:
  - `~/.claude/inject-state/` 配下の 24 時間以上古いセッション dedup ファイル（`.txt` / `.json`）
  - `~/.claude/edit-counter/` 配下の 24 時間以上古いセッション別カウントファイル
  - `~/.claude/edit-counter.txt`（旧グローバル形式）の 10000 行超ローテーション（後方互換）
