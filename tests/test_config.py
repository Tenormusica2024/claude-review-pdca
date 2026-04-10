"""
config.py のユニットテスト。
共有定数が正しい型・値を持つことを検証する。
"""
from pathlib import Path
from config import (
    ANTHROPIC_VERSION,
    DB_PATH,
    EDIT_COUNTER_DIR,
    GLM_CLASSIFIER_LOG_DIR,
    GLM_FALLBACK_LOG_PATH,
    GLM_API_URL,
    GLM_HTTP_429_SUPPRESSION_THRESHOLD,
    GLM_MODEL,
    GLM_SUPPRESSION_LOOKBACK,
    INJECT_STATE_DIR,
    ZAI_ANTHROPIC_BASE_URL,
    normalize_git_root,
)


class TestConfig:
    """config.py の定数が期待通りの値を持つことを検証。"""

    def test_db_path_is_path_object(self):
        """DB_PATH が Path オブジェクトである。"""
        assert isinstance(DB_PATH, Path)

    def test_db_path_ends_with_expected_name(self):
        """DB_PATH のファイル名が review-feedback.db である。"""
        assert DB_PATH.name == "review-feedback.db"

    def test_db_path_under_dot_claude(self):
        """DB_PATH が .claude ディレクトリ配下にある。"""
        assert ".claude" in DB_PATH.parts

    def test_inject_state_dir_is_path(self):
        """INJECT_STATE_DIR が Path オブジェクトである。"""
        assert isinstance(INJECT_STATE_DIR, Path)

    def test_inject_state_dir_name(self):
        """INJECT_STATE_DIR のディレクトリ名が inject-state である。"""
        assert INJECT_STATE_DIR.name == "inject-state"

    def test_edit_counter_dir_is_path(self):
        """EDIT_COUNTER_DIR が Path オブジェクトである。"""
        assert isinstance(EDIT_COUNTER_DIR, Path)

    def test_edit_counter_dir_name(self):
        """EDIT_COUNTER_DIR のディレクトリ名が edit-counter である。"""
        assert EDIT_COUNTER_DIR.name == "edit-counter"

    def test_glm_classifier_log_dir_name(self):
        """GLM fallback ログディレクトリ名。"""
        assert GLM_CLASSIFIER_LOG_DIR.name == "logs"

    def test_glm_fallback_log_path_name(self):
        """GLM fallback ログファイル名。"""
        assert GLM_FALLBACK_LOG_PATH.name == "glm-classifier-fallbacks.jsonl"

    def test_glm_suppression_lookback(self):
        """GLM 抑制判定の直近参照件数。"""
        assert GLM_SUPPRESSION_LOOKBACK == 10

    def test_glm_http_429_suppression_threshold(self):
        """GLM 抑制判定の 429 閾値。"""
        assert GLM_HTTP_429_SUPPRESSION_THRESHOLD == 3

    def test_zai_anthropic_base_url(self):
        """Z.ai の Anthropic 互換 base URL が共有設定にある。"""
        assert ZAI_ANTHROPIC_BASE_URL == "https://api.z.ai/api/anthropic"

    def test_glm_api_url(self):
        """GLM messages endpoint が base URL から組み立てられる。"""
        assert GLM_API_URL == "https://api.z.ai/api/anthropic/v1/messages"

    def test_glm_model(self):
        """hook 共通の GLM model 定義。"""
        assert GLM_MODEL == "glm-5.1"

    def test_anthropic_version(self):
        """Anthropic 互換 API version header の共有定義。"""
        assert ANTHROPIC_VERSION == "2023-06-01"


class TestNormalizeGitRoot:
    """normalize_git_root のエッジケーステスト。"""

    def test_backslash_to_forward_slash(self):
        """バックスラッシュがフォワードスラッシュに変換される。"""
        assert normalize_git_root("C:\\Users\\project\n") == "C:/Users/project"

    def test_trailing_newline_stripped(self):
        """末尾改行が除去される。"""
        assert normalize_git_root("/home/user/repo\n") == "/home/user/repo"

    def test_double_slash_removed(self):
        """二重スラッシュが単一スラッシュに正規化される。"""
        assert normalize_git_root("C://project//src\n") == "C:/project/src"

    def test_unc_path_preserved(self):
        """UNC パス先頭の // が保持される。"""
        assert normalize_git_root("//server/share\n") == "//server/share"

    def test_unc_path_internal_double_slash_removed(self):
        """UNC パス内部の二重スラッシュが除去される。"""
        assert normalize_git_root("//server//share//folder\n") == "//server/share/folder"

    def test_trailing_whitespace_stripped(self):
        """末尾の空白・タブが除去される。"""
        assert normalize_git_root("  /home/user/repo  \n") == "/home/user/repo"

    def test_mixed_separators(self):
        """バックスラッシュとフォワードスラッシュが混在していても正規化される。"""
        assert normalize_git_root("C:\\Users/project\\src\n") == "C:/Users/project/src"
