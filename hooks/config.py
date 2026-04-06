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
