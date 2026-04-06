"""
config.py のユニットテスト。
共有定数が正しい型・値を持つことを検証する。
"""
from pathlib import Path
from config import DB_PATH, INJECT_STATE_DIR, EDIT_COUNTER_DIR, normalize_git_root


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
