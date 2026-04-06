"""
config.py のユニットテスト。
共有定数が正しい型・値を持つことを検証する。
"""
from pathlib import Path
from config import DB_PATH, INJECT_STATE_DIR, EDIT_COUNTER_DIR


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
