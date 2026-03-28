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
    ├── Phase A: ファイル特化クエリ（SNR フィルタ: critical/high/warning・30日・NOT EXISTS）
    │       1件以上 → 注入文を stdout に出力 → Claude のコンテキストに追加
    │       0件     → Phase B へ
    │
    └── Phase B: プロジェクト横断フォールバック（severity=critical のみ・LIMIT 5）
            1件以上 → "PROJECT-WIDE CRITICAL PATTERNS" として注入
            0件     → exit 0（何も注入しない）
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
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path.home() / ".claude" / "review-feedback.db"
STATE_DIR = Path.home() / ".claude" / "inject-state"  # セッション別注入済みID管理ディレクトリ
INJECT_LIMIT = 8
FALLBACK_LIMIT = 5   # Phase B: プロジェクト横断 critical のみに絞るため小さめ
STALE_DAYS = 30


def _load_injected_ids(session_id: str) -> set[int]:
    """セッション内で既に注入済みの finding ID セットを読み込む（append-only .txt 形式）"""
    state_file = STATE_DIR / f"{session_id}.txt"
    if state_file.exists():
        ids = set()
        for line in state_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.isdigit():
                ids.add(int(line))
        return ids
    return set()


def _save_injected_ids(session_id: str, new_ids: set[int]) -> None:
    """注入済み finding ID を append-only でセッション状態ファイルに追記する（競合耐性）"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{session_id}.txt"
    # append-only: read-modify-write より並列プロセス競合に強い
    with open(state_file, "a", encoding="utf-8") as f:
        for id_ in new_ids:
            f.write(f"{id_}\n")


def _update_injection_tracking(conn: sqlite3.Connection, findings: list[dict], now: str) -> None:
    """注入した finding の injected_count / last_injected を更新（カラム未追加時はスキップ）"""
    if not findings:  # 空リストの場合は何もしない（placeholders が空になるのを防ぐ）
        return
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
    except sqlite3.OperationalError as e:
        if "injected_count" not in str(e) and "last_injected" not in str(e):
            # 想定外の OperationalError（DB破損等）は stderr に出力してデバッグを可能にする
            print(f"[pre-tool-inject-findings] OperationalError: {e}", file=sys.stderr)
        # injected_count / last_injected カラム未追加時は無視


def _get_project_root(file_path: str) -> str | None:
    """編集対象ファイルの git リポジトリルートを取得する（Phase B フィルタ用）"""
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(file_path).parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return result.stdout.strip().replace("\\", "/")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # git 管理外ファイルはプロジェクト判定不能 → None を返して Phase B をスキップ
    return None


def get_findings(file_path: str, session_id: str) -> tuple[list[dict], bool]:
    """
    findings と is_fallback フラグを返す。
    is_fallback=False: Phase A（ファイル特化）
    is_fallback=True : Phase B（プロジェクト横断 critical のみ・新規ファイル用フォールバック）

    セッション内 dedup: 同一セッションで既に注入した finding は再注入しない。
    各ツール呼び出しは独立プロセスなのでファイルベースで状態を管理する。
    """
    if not DB_PATH.exists():
        return [], False

    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).isoformat()
    now = datetime.now().isoformat()

    # session_id が空の場合は dedup をスキップ（unknown キーへの混在を防ぐ）
    if session_id:
        already_injected = _load_injected_ids(session_id)
    else:
        already_injected = set()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # --- Phase A: ファイル特化クエリ ---
        # ⚠️ dismissed カラムは Phase 1 ALTER TABLE 完了後に有効（追加前は OperationalError をスキップして空を返す）
        # NOT EXISTS サブクエリ: 同一パターンが既に 'fixed' になっている再発を除外（ノイズ削減）
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
            return [], False  # dismissed カラム未追加時はスキップ

        # セッション内 dedup: 既注入 ID を除外
        findings = [dict(r) for r in rows if dict(r)["id"] not in already_injected]
        if findings:
            # Phase 1 で ALTER TABLE が完了するまでカラムが存在しないため OperationalError をスキップ
            _update_injection_tracking(conn, findings, now)
            if session_id:
                _save_injected_ids(session_id, {f["id"] for f in findings})
            return findings, False

        # --- Phase B: プロジェクト横断 critical フォールバック（新規ファイル・findings なしファイル用）---
        # SNR 維持のため severity = 'critical' のみに絞り LIMIT も小さくする
        # project_root フィルタ: 他プロジェクトの critical を混入させない
        project_root = _get_project_root(file_path)
        project_filter = (project_root + "/%") if project_root else None

        if not project_filter:
            return [], False  # プロジェクト判定不能時は他プロジェクト混入防止のため Phase B をスキップ

        try:
            fallback_rows = conn.execute("""
                SELECT id, severity, category, finding_summary
                FROM findings
                WHERE dismissed = 0
                  AND resolution = 'pending'
                  AND severity = 'critical'
                  AND created_at >= ?
                  AND replace(file_path, '\\', '/') LIKE ?
                ORDER BY id DESC
                LIMIT ?
            """, (cutoff, project_filter, FALLBACK_LIMIT)).fetchall()
        except sqlite3.OperationalError:
            return [], False

        # セッション内 dedup（Phase B も同様に適用）
        fallback = [dict(r) for r in fallback_rows if dict(r)["id"] not in already_injected]
        if fallback:
            _update_injection_tracking(conn, fallback, now)
            if session_id:
                _save_injected_ids(session_id, {f["id"] for f in fallback})
            return fallback, True
        return [], False
    finally:
        conn.close()


def format_injection(file_path: str, findings: list[dict], is_fallback: bool = False) -> str:
    if is_fallback:
        # Phase B: 新規ファイル用（プロジェクト横断 critical のみ）
        header = f"=== PROJECT-WIDE CRITICAL PATTERNS (新規ファイル: {file_path}) ==="
    else:
        # Phase A: ファイル特化
        header = f"=== PAST FINDINGS: {file_path} ==="
    lines = [header]
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
    # MultiEdit は top-level に file_path を持たないため edits[0] から取得する（先頭ファイルのみ対象）
    if not file_path and tool_name == "MultiEdit":
        edits = tool_input.get("edits", [])
        if edits:
            file_path = edits[0].get("file_path")
    if not file_path:
        sys.exit(0)

    session_id = payload.get("session_id", "")  # 空文字列フォールバック（"unknown" にしない）
    findings, is_fallback = get_findings(file_path, session_id)
    if not findings:
        sys.exit(0)

    # stdout に注入文を出力 → Claude Code がコンテキストに追加する
    print(format_injection(file_path, findings, is_fallback))
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

if rows and (rows['critical'] or 0) > 0:
    print(f"🚨 CRITICAL findings: {rows['critical']} 件")
if rows and (rows['high'] or 0) > 0:
    print(f"⚠️ HIGH findings: {rows['high']} 件")
if rows and (rows['total'] or 0) > 0:
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

COUNTER_FILE = Path.home() / ".claude" / "edit-counter.txt"
BATCH_THRESHOLD = 5


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") not in ("Edit", "Write", "MultiEdit"):
        sys.exit(0)

    session_id = payload.get("session_id", "")
    if not session_id:
        sys.exit(0)  # session_id 不明時はスキップ

    # append-only: 1行=1イベント（read-modify-write 競合なし・JSON 破損リスクなし）
    COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COUNTER_FILE, "a", encoding="utf-8") as f:
        f.write(f"{session_id}\n")

    # 現セッションの累計カウントを行数で算出
    try:
        lines = COUNTER_FILE.read_text(encoding="utf-8").splitlines()
        count = lines.count(session_id)
    except OSError:
        count = 1

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
SessionEnd hook: ユーザー承認済み dismissed パターンを CLAUDE.md に追記 + inject-state クリーンアップ
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from datetime import date

DB_PATH = Path.home() / ".claude" / "review-feedback.db"
# PreToolUse hook のセッション別 dedup ファイル管理ディレクトリ
STATE_DIR = Path.home() / ".claude" / "inject-state"


def _find_claude_md(payload: dict) -> Path | None:
    """
    プロジェクト固有の CLAUDE.md を探す（3段フォールバック）。
    SessionEnd の cwd はプロジェクトルートとは限らないため動的に解決する。
    """
    # 1. payload["cwd"] を優先（Claude Code が渡す作業ディレクトリ）
    cwd_str = payload.get("cwd")
    if cwd_str:
        candidate = Path(cwd_str) / "CLAUDE.md"
        if candidate.exists():
            return candidate

    # 2. git rev-parse --show-toplevel で git root を取得
    try:
        search_dir = cwd_str or str(Path.cwd())
        result = subprocess.run(
            ["git", "-C", search_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            candidate = Path(result.stdout.strip()) / "CLAUDE.md"
            if candidate.exists():
                return candidate
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 3. Path.cwd() フォールバック
    candidate = Path.cwd() / "CLAUDE.md"
    if candidate.exists():
        return candidate

    return None  # プロジェクト固有 CLAUDE.md が見つからない場合はスキップ


def _cleanup_inject_state() -> None:
    """24時間以上古いセッション dedup ファイルを削除してストレージを管理する"""
    if STATE_DIR.exists():
        cutoff = time.time() - 86400  # 24時間
        # .txt（現行形式）と .json（旧形式の残骸）の両方を対象にする
        for pattern in ("*.txt", "*.json"):
            for f in STATE_DIR.glob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass  # 削除失敗は無視（別プロセスが使用中の可能性）

    # edit-counter.txt のローテーション（10000行超で古い半分を削除）
    counter_file = Path.home() / ".claude" / "edit-counter.txt"
    if counter_file.exists():
        try:
            lines = counter_file.read_text(encoding="utf-8").splitlines()
            if len(lines) > 10000:
                counter_file.write_text("\n".join(lines[-5000:]) + "\n", encoding="utf-8")
        except OSError:
            pass


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        payload = {}

    if not DB_PATH.exists():
        return

    # プロジェクト固有の CLAUDE.md を動的に探す（cwd 依存を排除）
    claude_md_path = _find_claude_md(payload)
    if claude_md_path is None:
        return  # プロジェクト固有 CLAUDE.md が存在しない場合はスキップ（グローバルは書かない）

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
        + "\n"  # 末尾改行: エディタの「末尾改行なし」警告を防ぐ
    )

    content = claude_md_path.read_text(encoding="utf-8")

    # 既存の自動追記ブロックを更新（重複防止）
    # splitlines() で行単位処理（CRLF 対応: re.DOTALL + \n^## パターンが CRLF で誤動作するのを防ぐ）
    marker = "## 学習済み false positive パターン"
    if any(line.startswith(marker) for line in content.splitlines()):
        lines = content.splitlines(keepends=True)
        result_lines = []
        in_block = False
        for line in lines:
            stripped = line.rstrip("\r\n")
            if stripped.startswith(marker):
                in_block = True
                continue
            if in_block and re.match(r'^#{1,6}\s', stripped):
                # 次のセクション開始で block 終了（##スペースなし見出し等も含めて検出）
                in_block = False
                result_lines.append(line)
                continue
            if not in_block:
                result_lines.append(line)
        content = "".join(result_lines).rstrip("\r\n")

    claude_md_path.write_text(content + block, encoding="utf-8")


if __name__ == "__main__":
    try:
        _cleanup_inject_state()
    except Exception:
        pass
    main()
```
