#!/usr/bin/env python3
"""
PostToolUse hook: 編集カウント管理（バッチレビュートリガー用）

セッション別ファイル（~/.claude/edit-counter/{session_id}.txt）を使用することで:
- カウントがセッション間で独立し、count() の集計コストも不要になる
- session-end-learn.py の cleanup で inject-state と一緒に削除できる
- グローバル単一ファイルによるカウント汚染が起きない
"""
import sys
import json
from pathlib import Path

# Windows 環境で cp932 stdout に日本語を出力するための UTF-8 強制
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

COUNTER_DIR = Path.home() / ".claude" / "edit-counter"
BATCH_THRESHOLD = 5
# セッション内の最大イベント数（これを超えたら古い半分を削除してローテーション）
ROTATION_LIMIT = 5000


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

    COUNTER_DIR.mkdir(parents=True, exist_ok=True)
    counter_file = COUNTER_DIR / f"{session_id}.txt"

    # append-only: 1行=1イベント（read-modify-write 競合なし・JSON 破損リスクなし）
    # 並列 Edit が同時に append してもファイル破損は発生しない
    # "e\n" を使う理由: "\n" だと splitlines() が空文字列を返すため、ローテーション後に
    # "\n" * N を書いても count=N になり偽通知が発火する既知バグを回避するため
    with open(counter_file, "a", encoding="utf-8") as f:
        f.write("e\n")

    # 非空行のみをイベントとしてカウント（"e" 行 = 1 イベント）
    try:
        lines = counter_file.read_text(encoding="utf-8").splitlines()
        count = sum(1 for line in lines if line)
    except OSError:
        count = 1

    # ローテーション: セッション内でも長期実行時に肥大化しないようにする
    # session-end の cleanup だけに頼るとクラッシュ時にファイルが残存するため二重対策
    # ゼロバイトリセット: ローテーション後の count=0 保証（"\n" * N は splitlines で count=N になるバグを回避）
    # ローテーション後は sys.exit(0) で即終了する（偽通知防止）
    if count > ROTATION_LIMIT:
        try:
            counter_file.write_text("", encoding="utf-8")
        except OSError:
            pass
        sys.exit(0)

    # BATCH_THRESHOLD に達したら通知（レビュー提案のみ・強制実行しない）
    if count > 0 and count % BATCH_THRESHOLD == 0:
        print(f"💡 {count} 件の編集が完了しました。/ifr でレビューを実行することを推奨します。")

    sys.exit(0)


if __name__ == "__main__":
    main()
