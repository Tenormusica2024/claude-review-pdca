"""
pattern_db.py のユニットテスト。

テスト対象:
- validate_category: カテゴリの正規化・エイリアス解決・フォールバック
- record_pattern: 新規パターン挿入 + 重複検出によるカウント増加
- get_patterns_for_file: cool-off フィルタ + カテゴリ別 top-1 選出
- format_injection_text: 注入テキスト生成
"""
import sqlite3
import pytest
from unittest.mock import patch
from pathlib import Path

from pattern_db import (
    validate_category,
    record_pattern,
    get_patterns_for_file,
    format_injection_text,
    VALID_CATEGORIES,
    PATTERNS_DB_PATH,
    _ensure_db,
    get_connection,
)


class TestValidateCategory:
    """カテゴリ正規化のテスト。"""

    def test_valid_category_passthrough(self):
        """正規カテゴリはそのまま返る。"""
        for cat in VALID_CATEGORIES:
            assert validate_category(cat) == cat

    def test_case_insensitive(self):
        """大文字混じりでも正規化される。"""
        assert validate_category("Logic") == "logic"
        assert validate_category("SECURITY") == "security"

    def test_underscore_to_hyphen(self):
        """アンダースコアがハイフンに変換される。"""
        assert validate_category("data_integrity") == "data-integrity"
        assert validate_category("type_safety") == "type-safety"

    def test_alias_resolution(self):
        """エイリアスが正規カテゴリに解決される。"""
        assert validate_category("bug") == "logic"
        assert validate_category("error-handling") == "robustness"
        assert validate_category("null-safety") == "security"
        assert validate_category("design") == "api-contract"
        assert validate_category("docs") == "documentation"
        assert validate_category("a11y") == "ux"
        assert validate_category("complexity") == "maintainability"

    def test_unknown_category_fallback(self):
        """未知のカテゴリは maintainability にフォールバック。"""
        assert validate_category("unknown-xyz") == "maintainability"

    def test_whitespace_handling(self):
        """前後の空白が除去される。"""
        assert validate_category("  logic  ") == "logic"


class TestRecordPattern:
    """パターン記録のテスト。"""

    @pytest.fixture(autouse=True)
    def _use_temp_db(self, tmp_path):
        """テスト用に一時 DB を使用する。"""
        test_db = tmp_path / "test-patterns.db"
        with patch("pattern_db.PATTERNS_DB_PATH", test_db):
            yield

    def test_insert_new_pattern(self):
        """新規パターンが挿入される。"""
        pid = record_pattern(
            category="logic",
            pattern_text="off-by-one in loop boundary",
            severity="warning",
            file_path="src/main.py",
            repo_root="C:/project",
        )
        assert pid is not None
        assert pid > 0

    def test_record_normalizes_repo_relative_path(self):
        """repo_root 配下の absolute path は relative path で保存される。"""
        pid = record_pattern(
            category="logic",
            pattern_text="boundary bug",
            file_path="C:/project/src/main.py",
            repo_root="C:/project",
        )
        conn = get_connection()
        try:
            row = conn.execute("SELECT file_path FROM patterns WHERE id = ?", (pid,)).fetchone()
            assert row["file_path"] == "src/main.py"
        finally:
            conn.close()

    def test_duplicate_increments_count(self):
        """同一パターンの再記録で detection_count が増加する。"""
        pid1 = record_pattern(
            category="logic",
            pattern_text="off-by-one in loop boundary",
            file_path="src/main.py",
            repo_root="C:/project",
        )
        pid2 = record_pattern(
            category="logic",
            pattern_text="off-by-one in loop boundary",
            file_path="src/main.py",
            repo_root="C:/project",
        )
        assert pid1 == pid2

        conn = get_connection()
        try:
            row = conn.execute("SELECT detection_count FROM patterns WHERE id = ?", (pid1,)).fetchone()
            assert row["detection_count"] == 2
        finally:
            conn.close()

    def test_pattern_text_truncation(self):
        """80文字を超えるパターンテキストが切り詰められる。"""
        long_text = "a" * 100
        pid = record_pattern(category="logic", pattern_text=long_text)
        conn = get_connection()
        try:
            row = conn.execute("SELECT pattern_text FROM patterns WHERE id = ?", (pid,)).fetchone()
            assert len(row["pattern_text"]) == 80
        finally:
            conn.close()

    def test_newline_removal(self):
        """パターンテキストから改行が除去される。"""
        pid = record_pattern(category="logic", pattern_text="line1\nline2\r\nline3")
        conn = get_connection()
        try:
            row = conn.execute("SELECT pattern_text FROM patterns WHERE id = ?", (pid,)).fetchone()
            assert "\n" not in row["pattern_text"]
            assert "\r" not in row["pattern_text"]
        finally:
            conn.close()

    def test_category_alias_normalized(self):
        """エイリアスカテゴリが正規化されて保存される。"""
        pid = record_pattern(category="bug", pattern_text="test")
        conn = get_connection()
        try:
            row = conn.execute("SELECT category FROM patterns WHERE id = ?", (pid,)).fetchone()
            assert row["category"] == "logic"
        finally:
            conn.close()

    def test_confidence_upgrade(self):
        """medium → high の再検出で confidence が high に昇格する。"""
        pid1 = record_pattern(
            category="logic", pattern_text="test pattern",
            file_path="a.py", confidence="medium",
        )
        pid2 = record_pattern(
            category="logic", pattern_text="test pattern",
            file_path="a.py", confidence="high",
        )
        assert pid1 == pid2
        conn = get_connection()
        try:
            row = conn.execute("SELECT confidence FROM patterns WHERE id = ?", (pid1,)).fetchone()
            assert row["confidence"] == "high"
        finally:
            conn.close()


class TestGetPatternsForFile:
    """ファイル別パターン取得のテスト。"""

    @pytest.fixture(autouse=True)
    def _use_temp_db(self, tmp_path):
        """テスト用に一時 DB を使用し、テストデータを投入する。"""
        test_db = tmp_path / "test-patterns.db"
        with patch("pattern_db.PATTERNS_DB_PATH", test_db):
            # テストデータ: detection_count >= 2 のパターンと < 2 のパターン
            conn = get_connection()
            try:
                # 学習済み（count=3）
                conn.execute("""
                    INSERT INTO patterns (category, pattern_text, severity, file_path, repo_root,
                                          confidence, detection_count, first_detected, last_detected)
                    VALUES ('logic', 'off-by-one error', 'warning', 'src/main.py', 'C:/project',
                            'high', 3, '2026-01-01T00:00:00', '2026-04-01T00:00:00')
                """)
                # 学習済み（count=2, critical）
                conn.execute("""
                    INSERT INTO patterns (category, pattern_text, severity, file_path, repo_root,
                                          confidence, detection_count, first_detected, last_detected)
                    VALUES ('security', 'SQL injection via string format', 'critical', 'src/main.py', 'C:/project',
                            'high', 2, '2026-01-01T00:00:00', '2026-03-01T00:00:00')
                """)
                # cool-off 未達（count=1）→ 取得されない
                conn.execute("""
                    INSERT INTO patterns (category, pattern_text, severity, file_path, repo_root,
                                          confidence, detection_count, first_detected, last_detected)
                    VALUES ('robustness', 'missing try-except', 'warning', 'src/main.py', 'C:/project',
                            'high', 1, '2026-01-01T00:00:00', '2026-04-01T00:00:00')
                """)
                conn.commit()
            finally:
                conn.close()
            yield

    def test_returns_learned_patterns(self):
        """detection_count >= 2 のパターンのみ返す。"""
        patterns = get_patterns_for_file("src/main.py", repo_root="C:/project")
        assert len(patterns) == 2

    def test_cool_off_filter(self):
        """detection_count < 2 のパターンは返さない。"""
        patterns = get_patterns_for_file("src/main.py", repo_root="C:/project")
        categories = {p["category"] for p in patterns}
        assert "robustness" not in categories

    def test_critical_first(self):
        """critical が warning より先に来る。"""
        patterns = get_patterns_for_file("src/main.py", repo_root="C:/project")
        assert patterns[0]["severity"] == "critical"

    def test_max_patterns_limit(self):
        """max_patterns で件数を制限できる。"""
        patterns = get_patterns_for_file("src/main.py", repo_root="C:/project", max_patterns=1)
        assert len(patterns) == 1

    def test_no_patterns_for_unknown_file(self):
        """未知のファイルには空リストを返す。"""
        patterns = get_patterns_for_file("unknown/file.py", repo_root="C:/project")
        assert patterns == []

    def test_backslash_normalization(self):
        """バックスラッシュのパスでも正しく取得できる。"""
        patterns = get_patterns_for_file("src\\main.py", repo_root="C:/project")
        assert len(patterns) == 2

    def test_matches_relative_rows_for_absolute_input(self):
        """absolute 入力でも relative 保存 rows を取得できる。"""
        patterns = get_patterns_for_file("C:/project/src/main.py", repo_root="C:/project")
        assert len(patterns) == 2


class TestFormatInjectionText:
    """注入テキスト生成のテスト。"""

    def test_empty_patterns(self):
        """空リストでは空文字列を返す。"""
        assert format_injection_text([]) == ""

    def test_basic_format(self):
        """基本的なフォーマットが正しい。"""
        patterns = [
            {"category": "logic", "pattern_text": "off-by-one", "severity": "warning", "count": 3},
        ]
        text = format_injection_text(patterns)
        assert "[LEARNED PATTERNS]" in text
        assert "[logic]" in text
        assert "off-by-one" in text
        assert "3回検出" in text

    def test_multiple_patterns(self):
        """複数パターンが正しく出力される。"""
        patterns = [
            {"category": "security", "pattern_text": "SQL injection", "severity": "critical", "count": 5},
            {"category": "logic", "pattern_text": "off-by-one", "severity": "warning", "count": 2},
        ]
        text = format_injection_text(patterns)
        assert "[security]" in text
        assert "[logic]" in text
        assert text.count("  - ") == 2
