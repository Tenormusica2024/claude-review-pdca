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

# Windows 環境で cp932 stdout に日本語を出力するための UTF-8 強制
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
    """注入済み finding ID を append-only でセッション状態ファイルに追記する（競合耐性）。

    並列 Edit 実行時に複数プロセスが同時に append する競合が発生し得るが、
    1行 append は OS レベルで原子的なためファイル破損は発生しない。
    最悪ケースでも同一 finding が 1 回余分に注入されるだけ（SNR への影響は軽微）。
    厳密な排他制御が必要な場合はファイルロックを追加する。
    """
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
            # バックスラッシュをスラッシュに統一し、UNCパス（\\server\share）の
            # 二重スラッシュ（//server/share）も除去して LIKE パターンとの整合性を保つ
            normalized = result.stdout.strip().replace("\\", "/")
            return normalized.replace("//", "/") if normalized.startswith("//") else normalized
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

    # DB の created_at は SCHEMA で strftime('%Y-%m-%dT%H:%M:%S','now','localtime') 形式
    # Python 側も T 区切りに統一することで文字列比較の正確性を保証する
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    # session_id が空の場合は注入をスキップする。
    # dedup なしで毎回全件を注入すると SNR が破壊されるため、
    # session_id を取得できない環境では注入しない方が安全。
    if not session_id:
        return [], False
    already_injected = _load_injected_ids(session_id)

    # timeout=5: 並列プロセスによる SQLITE_BUSY を待機して解消する
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        # --- Phase A: ファイル特化クエリ ---
        # NOT EXISTS サブクエリ: 同一ファイル内で同一パターンが既に 'fixed' の場合に除外（再発ノイズ削減）
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
            return [], False  # dismissed カラム未追加時はスキップ

        # セッション内 dedup: 既注入 ID を除外
        findings = [dict(r) for r in rows if dict(r)["id"] not in already_injected]
        if findings:
            # Phase 1 で ALTER TABLE が完了するまでカラムが存在しないため OperationalError をスキップ
            _update_injection_tracking(conn, findings, now)
            _save_injected_ids(session_id, {f["id"] for f in findings})
            return findings, False

        # --- Phase B: プロジェクト横断 critical フォールバック（新規ファイル・findings なしファイル用）---
        # SNR 維持のため severity = 'critical' のみに絞り LIMIT も小さくする
        # project_root フィルタ: 他プロジェクトの critical を混入させない
        project_root = _get_project_root(file_path)
        # rstrip('/') で末尾スラッシュを正規化してから '/%' を付与（境界不一致防止）
        # project_filter 自体は LOWER() 変換しない。クエリ側で両辺を LOWER() 適用するため
        project_filter = (project_root.rstrip("/") + "/%") if project_root else None

        if not project_filter:
            return [], False  # プロジェクト判定不能時は他プロジェクト混入防止のため Phase B をスキップ

        # depth check: git root が浅すぎる（ドライブルートや home 直下等）場合は
        # 他プロジェクトの findings を大量に巻き込む恐れがあるためスキップ
        # Windows: "C:/Users/foo/project" → parts = ('C:\\', 'Users', 'foo', 'project') → len=4
        # Linux: "/home/user/project"    → parts = ('/', 'home', 'user', 'project')      → len=4
        # 最低 4 セグメント未満（例: C:\Users まで）はプロジェクトルートとして認めない
        if len(Path(project_root).parts) < 4:
            return [], False

        try:
            fallback_rows = conn.execute("""
                SELECT id, severity, category, finding_summary
                FROM findings
                WHERE dismissed = 0
                  AND resolution = 'pending'
                  AND severity = 'critical'
                  AND created_at >= ?
                  AND LOWER(replace(file_path, '\\', '/')) LIKE LOWER(?)
                ORDER BY id DESC
                LIMIT ?
            """, (cutoff, project_filter, FALLBACK_LIMIT)).fetchall()
        except sqlite3.OperationalError:
            return [], False

        # セッション内 dedup（Phase B も同様に適用）
        fallback = [dict(r) for r in fallback_rows if dict(r)["id"] not in already_injected]
        if fallback:
            _update_injection_tracking(conn, fallback, now)
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

    # 対象ファイルパスを収集する
    # MultiEdit: 全 edits から重複除去して収集（先頭のみ対象にすると他ファイルの findings が見落とされる）
    file_paths: list[str] = []
    if tool_name == "MultiEdit":
        seen: set[str] = set()
        for edit in tool_input.get("edits", []):
            fp = edit.get("file_path")
            if fp and fp not in seen:
                file_paths.append(fp)
                seen.add(fp)
    else:
        fp = tool_input.get("file_path") or tool_input.get("path")
        if fp:
            file_paths = [fp]

    if not file_paths:
        sys.exit(0)

    session_id = payload.get("session_id", "")  # 空文字列フォールバック（"unknown" にしない）

    # 各ファイルの findings を収集し、出力があるものだけ注入する
    outputs = []
    for fp in file_paths:
        findings, is_fallback = get_findings(fp, session_id)
        if findings:
            outputs.append(format_injection(fp, findings, is_fallback))

    if not outputs:
        sys.exit(0)

    # stdout に注入文を出力 → Claude Code がコンテキストに追加する
    print("\n\n".join(outputs))
    sys.exit(0)


if __name__ == "__main__":
    main()
