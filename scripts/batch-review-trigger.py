#!/usr/bin/env python3
"""
scripts/batch-review-trigger.py

5編集ごとのバッチレビュー支援スクリプト。

役割:
  PostToolUse hook が「5件の編集が完了しました」と通知した後、Claude がどのファイルを
  どの優先度でレビューすべきかを DB から構造化して出力する。
  /ifr や /review 実行前のコンテキスト補強用途で使う。

使用方法:
  python batch-review-trigger.py [--session-id SESSION_ID] [--project-root PATH] [--check-only]

オプション:
  --session-id SESSION_ID  対象セッション ID（省略時は CLAUDE_SESSION_ID 環境変数を参照）
  --project-root PATH      プロジェクトルートパス（省略時は CWD）
  --check-only             edit count のみ確認して終了（findings 出力なし）
  --threshold N            バッチレビューのトリガー閾値（デフォルト: 5）

exit code:
  0: 正常終了
  1: エラー（DB 未存在 / 読み取り失敗等）
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# hook は任意の cwd から実行されるため、config.py がある hooks/ を sys.path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

# Windows 環境で cp932 stdout に日本語を出力するための UTF-8 強制
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import DB_PATH, EDIT_COUNTER_DIR as COUNTER_DIR
DEFAULT_THRESHOLD = 5
STALE_DAYS = 30
# バッチレビュー用: より広めに取得（ファイル単位ではなくプロジェクト横断）
BATCH_FINDINGS_LIMIT = 20


def _get_edit_count(session_id: str) -> int:
    """セッションの累計 edit count をカウントファイルから読み込む。"""
    counter_file = COUNTER_DIR / f"{session_id}.txt"
    if not counter_file.exists():
        return 0
    try:
        lines = counter_file.read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines if line)
    except OSError:
        return 0


def _get_edited_files(session_id: str) -> list[str]:
    """セッション内で編集されたファイルリストを取得する。"""
    files_file = COUNTER_DIR / f"{session_id}_files.txt"
    if not files_file.exists():
        return []
    try:
        return [line.strip() for line in files_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def _reset_counter(session_id: str) -> None:
    """レビュー後にカウンタとファイルリストをリセットする。"""
    counter_file = COUNTER_DIR / f"{session_id}.txt"
    files_file = COUNTER_DIR / f"{session_id}_files.txt"
    try:
        counter_file.write_text("", encoding="utf-8")
    except OSError:
        pass
    try:
        files_file.write_text("", encoding="utf-8")
    except OSError:
        pass


def _get_pending_findings(project_root: str | None) -> list[dict]:
    """
    プロジェクト内の pending findings を severity 降順で取得する。

    project_root が None の場合はプロジェクトフィルタなし（全体）。
    """
    if not DB_PATH.exists():
        return []

    # pre-tool-inject-findings.py と統一: DB の created_at (秒精度) との文字列比較で誤差を防ぐ
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')

    conn = None  # sqlite3.connect() 失敗時の finally NameError を防ぐ
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        if project_root:
            # パス区切りを / に正規化して LIKE フィルタ
            normalized_root = project_root.replace("\\", "/")
            project_filter = normalized_root + "/%"
            try:
                rows = conn.execute("""
                    SELECT id, severity, category, file_path, finding_summary
                    FROM findings
                    WHERE dismissed = 0
                      AND resolution = 'pending'
                      AND severity IN ('critical', 'high', 'warning')
                      AND (created_at >= ? OR COALESCE(last_relevant_edit, '2000-01-01') >= ?)
                      AND LOWER(replace(file_path, '\\', '/')) LIKE LOWER(?)
                    ORDER BY
                      CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'high'     THEN 1
                        WHEN 'warning'  THEN 2
                        ELSE 3
                      END,
                      id DESC
                    LIMIT ?
                """, (cutoff, cutoff, project_filter, BATCH_FINDINGS_LIMIT)).fetchall()
            except sqlite3.OperationalError as e:
                print(f"[batch-review-trigger] DB クエリエラー: {e}", file=sys.stderr)
                return []
        else:
            try:
                rows = conn.execute("""
                    SELECT id, severity, category, file_path, finding_summary
                    FROM findings
                    WHERE dismissed = 0
                      AND resolution = 'pending'
                      AND severity IN ('critical', 'high', 'warning')
                      AND (created_at >= ? OR COALESCE(last_relevant_edit, '2000-01-01') >= ?)
                    ORDER BY
                      CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'high'     THEN 1
                        WHEN 'warning'  THEN 2
                        ELSE 3
                      END,
                      id DESC
                    LIMIT ?
                """, (cutoff, cutoff, BATCH_FINDINGS_LIMIT)).fetchall()
            except sqlite3.OperationalError as e:
                print(f"[batch-review-trigger] DB クエリエラー: {e}", file=sys.stderr)
                return []

        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        print(f"[batch-review-trigger] DB 接続エラー: {e}", file=sys.stderr)
        return []
    finally:
        if conn:
            conn.close()


def _format_batch_report(findings: list[dict], edit_count: int, threshold: int, edited_files: list[str] | None = None) -> str:
    """batch review 用の構造化レポートを生成する。"""
    lines = [
        f"=== BATCH REVIEW TRIGGER: {edit_count} 件の編集が完了 ===",
        f"（閾値 {threshold} 件に達しました。以下の pending findings を参考にレビューしてください）",
        "",
    ]

    if edited_files:
        lines.append(f"レビュー対象ファイル（セッション内全編集 {len(edited_files)} 件）:")
        for fp in edited_files:
            lines.append(f"  - {fp}")
        lines.append("")

    if not findings:
        lines.append("pending findings: なし（DB にマッチする findings が存在しない）")
        lines.append("=== END BATCH REVIEW ===")
        return "\n".join(lines)

    # ファイルごとにグループ化
    by_file: dict[str, list[dict]] = {}
    for f in findings:
        fp = f.get("file_path") or "(unknown)"
        by_file.setdefault(fp, []).append(f)

    # severity 集計
    critical_count = sum(1 for f in findings if f.get("severity") == "critical")
    high_count = sum(1 for f in findings if f.get("severity") == "high")
    warning_count = sum(1 for f in findings if f.get("severity") == "warning")

    lines.append(
        f"pending findings: {len(findings)} 件"
        f"（critical: {critical_count}, high: {high_count}, warning: {warning_count}）"
    )
    lines.append("")

    for file_path, file_findings in sorted(
        by_file.items(),
        # 1次キー: severity優先度（critical→high→warning）、2次キー: file_path で安定ソート
        key=lambda kv: (
            (0 if any(f.get("severity") == "critical" for f in kv[1]) else
             1 if any(f.get("severity") == "high" for f in kv[1]) else 2),
            kv[0]  # file_path: 同一優先度内での出力順序を一定に保つ
        )
    ):
        lines.append(f"[FILE] {file_path}")
        for f in file_findings:
            sev = f.get("severity", "?").upper()
            cat = f.get("category") or "?"
            summary = f.get("finding_summary") or "(no summary)"
            lines.append(f"  [{sev}] {cat}: {summary}")
        lines.append("")

    lines.append("=== END BATCH REVIEW ===")
    lines.append(
        "上記を参考に /ifr または手動レビューを実行してください。"
        " 新しい findings は `review-feedback.py record` で DB に保存されます。"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="バッチレビュートリガー: 5編集ごとに pending findings を構造化出力する"
    )
    parser.add_argument(
        "--session-id",
        default=os.environ.get("CLAUDE_SESSION_ID", ""),
        help="対象セッション ID（省略時は CLAUDE_SESSION_ID 環境変数）",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="プロジェクトルートパス（省略時は全プロジェクト横断）",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="edit count のみ確認して終了（findings 出力なし）",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"バッチレビューのトリガー閾値（デフォルト: {DEFAULT_THRESHOLD}）",
    )
    args = parser.parse_args()

    session_id = args.session_id
    threshold = args.threshold

    # セッション ID なしでも動作可能（count=0 として扱い、findings だけ出力する）
    if session_id:
        edit_count = _get_edit_count(session_id)
    else:
        # session_id 不明時は count を算出できないため 0 扱い
        # --check-only では意味がないためスキップ
        edit_count = 0

    if args.check_only:
        if session_id:
            print(f"edit count: {edit_count} / {threshold}")
        else:
            print("session-id が指定されていないため edit count を取得できません。")
            print("--session-id か CLAUDE_SESSION_ID 環境変数を設定してください。")
        return 0

    # threshold 未達かつ session_id が指定されている場合は通知なしで終了
    # edit_count == 0 も含めてチェックする（カウントファイル未生成 = 編集ゼロ時も findings 出力をスキップ）
    if session_id and edit_count < threshold:
        if edit_count > 0:
            # 残りカウントを表示（デバッグ・モニタリング用）
            remaining = threshold - edit_count
            print(f"[batch-review-trigger] edit count: {edit_count} / {threshold} (あと {remaining} 件でバッチレビュー)")
        return 0

    # DB が存在しない場合は早期終了
    if not DB_PATH.exists():
        print(f"[batch-review-trigger] DB が見つかりません: {DB_PATH}", file=sys.stderr)
        return 1

    # セッション内で編集されたファイルリストを取得
    edited_files = _get_edited_files(session_id) if session_id else []

    # findings を取得して出力
    project_root = args.project_root
    findings = _get_pending_findings(project_root)
    report = _format_batch_report(findings, edit_count, threshold, edited_files)
    print(report)

    # レビュー後にカウンタとファイルリストをリセット（次の5編集サイクルへ）
    if session_id:
        _reset_counter(session_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
