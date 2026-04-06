"""
共通設定: 全 hook で共有する定数を一箇所で管理する。
DB パスやディレクトリパスの変更時に複数ファイルの同時修正を不要にする。
"""
from pathlib import Path

# SQLite DB パス（review-feedback.py と共通）
DB_PATH = Path.home() / ".claude" / "review-feedback.db"

# PreToolUse hook: セッション別注入済み ID 管理ディレクトリ
INJECT_STATE_DIR = Path.home() / ".claude" / "inject-state"

# PostToolUse hook: セッション別編集カウントディレクトリ
EDIT_COUNTER_DIR = Path.home() / ".claude" / "edit-counter"


def normalize_git_root(raw_output: str) -> str:
    """git rev-parse --show-toplevel の出力を正規化する。
    バックスラッシュ→スラッシュ統一、二重スラッシュ除去、UNCパス先頭 // 保持。"""
    normalized = raw_output.strip().replace("\\", "/")
    # UNCパス（//server/share）の先頭 // は保持し、内部の // のみ除去
    unc_prefix = ""
    if normalized.startswith("//"):
        unc_prefix = "//"
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return unc_prefix + normalized
