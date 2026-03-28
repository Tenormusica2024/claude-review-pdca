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
    │       MultiEdit → tool_input["edits"][0]["file_path"]（先頭ファイルのみ対象）
    │
    ├── SQLite クエリ（ファイル特化・SNR フィルタ）
    │
    ├── 0件 → exit 0（何も注入しない）
    │
    └── N件 → 注入文を stdout に出力 → Claude のコンテキストに追加
```

### 実装（hooks/pre-tool-inject-findings.py）

```python
#!/usr/bin/env python3
"""
PreToolUse hook: Edit/Write 実行時に file_path 特化 findings を注入する
"""
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path.home() / ".claude" / "review-feedback.db"
INJECT_LIMIT = 8
STALE_DAYS = 30


def get_findings(file_path: str) -> list[dict]:
    if not DB_PATH.exists():
        return []

    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).isoformat()
    now = datetime.now().isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # ⚠️ dismissed カラムは Phase 1 ALTER TABLE 完了後に有効（追加前は OperationalError をスキップして空を返す）
        try:
            rows = conn.execute("""
                SELECT id, severity, category, finding_summary
                FROM findings
                WHERE file_path = ?
                  AND dismissed = 0
                  AND resolution = 'pending'
                  AND severity IN ('critical', 'high', 'warning')
                  AND created_at >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM findings f2
                      WHERE f2.file_path       = findings.file_path
                        AND f2.category        = findings.category
                        AND f2.finding_summary = findings.finding_summary
                        AND f2.resolution      = 'fixed'
                  )
                ORDER BY
                  CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high'     THEN 1
                    WHEN 'warning'  THEN 2
                    ELSE 3
                  END,
                  id DESC
                LIMIT ?
            """, (file_path, cutoff, INJECT_LIMIT)).fetchall()
        except sqlite3.OperationalError:
            return []  # dismissed カラム未追加時はスキップ
        findings = [dict(r) for r in rows]

        # 注入トラッキング: 注入した finding の injected_count と last_injected を更新
        # Phase 1 で ALTER TABLE が完了するまでカラムが存在しないため OperationalError をスキップ
        if findings:
            ids = [f["id"] for f in findings]
            placeholders = ",".join("?" * len(ids))
            try:
                conn.execute(f"""
                    UPDATE findings
                    SET injected_count = injected_count + 1,
                        last_injected  = ?
                    WHERE id IN ({placeholders})
                """, (now,) + tuple(ids))
                conn.commit()
            except sqlite3.OperationalError:
                pass  # injected_count / last_injected カラム未追加時はスキップ

        return findings
    finally:
        conn.close()


def format_injection(file_path: str, findings: list[dict]) -> str:
    lines = [f"=== PAST FINDINGS: {file_path} ==="]
    for f in findings:
        lines.append(f"【{f['severity']}】{f['category'] or '?'}: {f['finding_summary']}")
    lines.append(f"（{len(findings)} 件を表示）")
    lines.append("これらを考慮して編集してください。同じアンチパターンの繰り返しは避けること。")
    lines.append("=== END FINDINGS ===")
    return "\n".join(lines)


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    # Edit/Write/MultiEdit のみ対象
    if tool_name not in ("Edit", "Write", "MultiEdit"):
        sys.exit(0)

    file_path = tool_input.get("file_path") or tool_input.get("path")
    # MultiEdit は edits リストの最初の要素から file_path を取る
    if not file_path and tool_name == "MultiEdit":
        edits = tool_input.get("edits", [])
        if edits:
            file_path = edits[0].get("file_path")
    if not file_path:
        sys.exit(0)

    findings = get_findings(file_path)
    if not findings:
        sys.exit(0)

    # stdout に注入文を出力 → Claude Code がコンテキストに追加する
    print(format_injection(file_path, findings))
    sys.exit(0)


if __name__ == "__main__":
    main()
```

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
# 改善版（既存スクリプトに差し替え）
# ⚠️ dismissed カラムは Phase 1 ALTER TABLE 完了後に有効（追加前は AND dismissed = 0 を除くこと）
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
    rows = None  # dismissed カラム未追加時はスキップ

if rows and rows['critical'] > 0:
    print(f"🚨 CRITICAL findings: {rows['critical']} 件")
if rows and rows['high'] > 0:
    print(f"⚠️ HIGH findings: {rows['high']} 件")
if rows and rows['total'] > 0:
    print(f"（全 {rows['total']} 件: python review-feedback.py query --resolution pending）")
```

---

## 3. PostToolUse: 編集カウント管理

毎回 /ifr を呼ばない。カウントファイルで管理し、5回ごとに提案する。

```python
#!/usr/bin/env python3
"""
PostToolUse hook: 編集カウント管理（バッチレビュートリガー用）
"""
import sys
import json
from pathlib import Path

COUNTER_FILE = Path.home() / ".claude" / "edit-counter.json"
BATCH_THRESHOLD = 5


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") not in ("Edit", "Write", "MultiEdit"):
        sys.exit(0)

    # カウンタ更新（JSON 破損時は空辞書にリセット）
    data = {}
    if COUNTER_FILE.exists():
        try:
            data = json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            data = {}

    session_id = payload.get("session_id", "unknown")
    count = data.get(session_id, 0) + 1
    data[session_id] = count
    COUNTER_FILE.write_text(json.dumps(data), encoding="utf-8")

    # BATCH_THRESHOLD に達したら通知（レビュー提案のみ・強制実行しない）
    if count % BATCH_THRESHOLD == 0:
        print(f"💡 {count} 件の編集が完了しました。/ifr でレビューを実行することを推奨します。")

    sys.exit(0)
```

---

## 4. SessionEnd: dismissed 学習 → CLAUDE.md 追記

```python
#!/usr/bin/env python3
"""
SessionEnd hook: ユーザー承認済み dismissed パターンを CLAUDE.md に追記
"""
import re
import sqlite3
from pathlib import Path
from datetime import date

DB_PATH = Path.home() / ".claude" / "review-feedback.db"
# グローバル CLAUDE.md ではなくセッションのカレントプロジェクト固有の CLAUDE.md に書く
CLAUDE_MD_PATH = Path.cwd() / "CLAUDE.md"

def main():
    if not DB_PATH.exists():
        return
    # プロジェクト固有の CLAUDE.md が存在しない場合はスキップ（グローバル CLAUDE.md は書かない）
    if not CLAUDE_MD_PATH.exists():
        return

    # with 構文でコネクションを自動クローズ
    try:
        with sqlite3.connect(DB_PATH) as conn:
            # Phase 1 ALTER TABLE 完了前は dismissed カラムが存在しないためスキップ
            try:
                rows = conn.execute("""
                    SELECT category, fp_reason, COUNT(*) AS cnt
                    FROM findings
                    WHERE dismissed = 1
                      AND dismissed_by = 'user'
                      AND fp_reason IS NOT NULL
                      AND fp_reason != ''
                    GROUP BY category, fp_reason
                    HAVING cnt >= 2     -- 2回以上承認されたパターンのみ学習
                    ORDER BY cnt DESC
                    LIMIT 10
                """).fetchall()
            except sqlite3.OperationalError:
                return  # dismissed カラム未追加時はスキップ
    except sqlite3.Error:
        return

    if not rows:
        return

    new_entries = [f"- [{r[0]}] {r[1]} （{r[2]}回承認）" for r in rows]
    block = (
        "\n\n## 学習済み false positive パターン（自動追記: "
        + str(date.today())
        + "）\n"
        + "\n".join(new_entries)
    )

    content = CLAUDE_MD_PATH.read_text(encoding="utf-8")

    # 既存の自動追記ブロックを更新（重複防止）
    # 行頭マッチで検索し、次の ## セクションまでを正確に置換（後続セクション誤削除防止）
    if re.search(r'^## 学習済み false positive パターン', content, re.MULTILINE):
        pattern = re.compile(
            r'^## 学習済み false positive パターン.*?(?=\n^## |\Z)',
            re.DOTALL | re.MULTILINE
        )
        content = pattern.sub('', content).rstrip()

    CLAUDE_MD_PATH.write_text(content + block, encoding="utf-8")

if __name__ == "__main__":
    main()
```
