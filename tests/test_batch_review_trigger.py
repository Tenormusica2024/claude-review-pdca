"""
batch-review-trigger.py のユニットテスト。

テスト対象の主な関数:
- _get_edit_count: セッションの累計 edit count
- _get_edited_files: セッション内で編集されたファイルリスト
- _reset_counter: レビュー後のカウンタリセット
- _get_pending_findings: pending findings の取得
- _format_batch_report: バッチレビュー用レポート生成
"""
import importlib
import pytest
from pathlib import Path
from unittest.mock import patch

trigger_mod = importlib.import_module("batch-review-trigger")

_get_edit_count = trigger_mod._get_edit_count
_get_edited_files = trigger_mod._get_edited_files
_reset_counter = trigger_mod._reset_counter
_get_pending_findings = trigger_mod._get_pending_findings
_format_batch_report = trigger_mod._format_batch_report


class TestGetEditCount:
    """edit count 読み込みのテスト。"""

    def test_nonexistent_session(self, tmp_path):
        """存在しないセッションは 0 を返す。"""
        with patch.object(trigger_mod, "COUNTER_DIR", tmp_path):
            assert _get_edit_count("nonexistent") == 0

    def test_counts_nonempty_lines(self, tmp_path):
        """非空行のみカウントする。"""
        counter_file = tmp_path / "sess1.txt"
        counter_file.write_text("e\ne\n\ne\n", encoding="utf-8")
        with patch.object(trigger_mod, "COUNTER_DIR", tmp_path):
            assert _get_edit_count("sess1") == 3

    def test_empty_file(self, tmp_path):
        """空ファイルは 0 を返す。"""
        counter_file = tmp_path / "empty.txt"
        counter_file.write_text("", encoding="utf-8")
        with patch.object(trigger_mod, "COUNTER_DIR", tmp_path):
            assert _get_edit_count("empty") == 0


class TestGetEditedFiles:
    """編集ファイルリスト取得のテスト。"""

    def test_returns_file_list(self, tmp_path):
        """ファイルリストを正しく返す。"""
        files_file = tmp_path / "sess1_files.txt"
        files_file.write_text("C:/a.py\nC:/b.py\n", encoding="utf-8")
        with patch.object(trigger_mod, "COUNTER_DIR", tmp_path):
            result = _get_edited_files("sess1")
        assert result == ["C:/a.py", "C:/b.py"]

    def test_empty_returns_empty(self, tmp_path):
        """ファイルが存在しない場合は空リスト。"""
        with patch.object(trigger_mod, "COUNTER_DIR", tmp_path):
            assert _get_edited_files("nonexistent") == []


class TestResetCounter:
    """カウンタリセットのテスト。"""

    def test_resets_both_files(self, tmp_path):
        """カウンタとファイルリスト両方がリセットされる。"""
        counter_file = tmp_path / "sess1.txt"
        files_file = tmp_path / "sess1_files.txt"
        counter_file.write_text("e\ne\n", encoding="utf-8")
        files_file.write_text("C:/a.py\n", encoding="utf-8")

        with patch.object(trigger_mod, "COUNTER_DIR", tmp_path):
            _reset_counter("sess1")

        assert counter_file.read_text(encoding="utf-8") == ""
        assert files_file.read_text(encoding="utf-8") == ""


class TestGetPendingFindings:
    """pending findings 取得のテスト。"""

    @pytest.mark.skip(reason="DB 依存の統合テストが必要。in-memory DB では DB_PATH の exists チェックをバイパスできない")
    def test_returns_pending_findings(self, sample_findings):
        """pending + severity IN (critical, high, warning) の findings を返す。"""
        pass

    def test_returns_empty_without_db(self, tmp_path):
        """DB が存在しない場合は空リスト。"""
        with patch.object(trigger_mod, "DB_PATH", tmp_path / "nonexistent.db"):
            result = _get_pending_findings("C:/project")
        assert result == []


class TestFormatBatchReport:
    """バッチレポート生成のテスト。"""

    def test_header_includes_count(self):
        """ヘッダーに edit count が表示される。"""
        report = _format_batch_report([], 10, 5)
        assert "10 件の編集が完了" in report

    def test_no_findings_message(self):
        """findings が 0 件のときの表示。"""
        report = _format_batch_report([], 5, 5)
        assert "pending findings: なし" in report

    def test_findings_grouped_by_file(self):
        """findings がファイルごとにグループ化される。"""
        findings = [
            {"severity": "critical", "category": "security", "file_path": "C:/a.py", "finding_summary": "XSS"},
            {"severity": "warning", "category": "style", "file_path": "C:/b.py", "finding_summary": "Naming"},
        ]
        report = _format_batch_report(findings, 5, 5)
        assert "[FILE] C:/a.py" in report
        assert "[FILE] C:/b.py" in report

    def test_severity_counts(self):
        """severity 集計が正しい。"""
        findings = [
            {"severity": "critical", "category": "a", "file_path": "/a.py", "finding_summary": "x"},
            {"severity": "high", "category": "b", "file_path": "/b.py", "finding_summary": "y"},
            {"severity": "warning", "category": "c", "file_path": "/c.py", "finding_summary": "z"},
        ]
        report = _format_batch_report(findings, 5, 5)
        assert "critical: 1" in report
        assert "high: 1" in report
        assert "warning: 1" in report

    def test_edited_files_shown(self):
        """編集ファイルリストが表示される。"""
        report = _format_batch_report([], 5, 5, edited_files=["C:/x.py", "C:/y.py"])
        assert "C:/x.py" in report
        assert "C:/y.py" in report

    def test_severity_sort_order(self):
        """critical → high → warning の順でソートされる。"""
        findings = [
            {"severity": "warning", "category": "a", "file_path": "/c.py", "finding_summary": "low"},
            {"severity": "critical", "category": "b", "file_path": "/a.py", "finding_summary": "crit"},
        ]
        report = _format_batch_report(findings, 5, 5)
        # critical のファイルが先に出る
        crit_pos = report.index("[FILE] /a.py")
        warn_pos = report.index("[FILE] /c.py")
        assert crit_pos < warn_pos
