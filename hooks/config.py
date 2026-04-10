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

# GLM classifier fallback の append-only ログディレクトリ
GLM_CLASSIFIER_LOG_DIR = Path.home() / ".claude" / "logs"
GLM_FALLBACK_LOG_PATH = GLM_CLASSIFIER_LOG_DIR / "glm-classifier-fallbacks.jsonl"
GLM_SUPPRESSION_LOOKBACK = 10
GLM_HTTP_429_SUPPRESSION_THRESHOLD = 3

# review-feedback.py CLI スクリプトパス（dismiss コマンド等で参照）
REVIEW_FEEDBACK_SCRIPT = str(Path.home() / ".claude" / "scripts" / "review-feedback.py")

# GLM 分類・軽量 hook 用の Z.ai Anthropic 互換 API 設定
ZAI_ANTHROPIC_BASE_URL = "https://api.z.ai/api/anthropic"
GLM_API_URL = f"{ZAI_ANTHROPIC_BASE_URL}/v1/messages"
GLM_MODEL = "glm-5.1"
ANTHROPIC_VERSION = "2023-06-01"


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
