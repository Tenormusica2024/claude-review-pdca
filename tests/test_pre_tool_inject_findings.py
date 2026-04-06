"""
pre-tool-inject-findings.py のユニットテスト。

テスト対象の主な関数:
- _load_injected_ids / _save_injected_ids: ファイルベースのセッション dedup
- get_findings: Phase A（ファイル特化）/ Phase B（プロジェクト横断 critical フォールバック）
- get_fp_patterns: 学習済み FP パターン取得
- format_injection: 注入テキスト生成
- _update_injection_tracking: 注入回数の更新
"""
import sqlite3
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

# テスト対象モジュール（sys.path は conftest.py で設定済み）
import importlib
inject_mod = importlib.import_module("pre-tool-inject-findings")

_load_injected_ids = inject_mod._load_injected_ids
_save_injected_ids = inject_mod._save_injected_ids
_update_injection_tracking = inject_mod._update_injection_tracking
get_findings = inject_mod.get_findings
get_fp_patterns = inject_mod.get_fp_patterns
format_injection = inject_mod.format_injection


class TestLoadSaveInjectedIds:
    """セッション dedup ファイルの読み書きテスト。"""

    def test_load_empty_session(self, tmp_path):
        """存在しないセッションファイルは空セットを返す。"""
        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            result = _load_injected_ids("nonexistent_session")
        assert result == set()

    def test_save_and_load_roundtrip(self, tmp_path):
        """保存した ID が正しく読み込まれる。"""
        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            _save_injected_ids("test_sess", {1, 2, 3})
            result = _load_injected_ids("test_sess")
        assert result == {1, 2, 3}

    def test_save_appends(self, tmp_path):
        """複数回の save が append される。"""
        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            _save_injected_ids("test_sess", {1, 2})
            _save_injected_ids("test_sess", {3, 4})
            result = _load_injected_ids("test_sess")
        assert result == {1, 2, 3, 4}

    def test_rotation_on_large_file(self, tmp_path):
        """DEDUP_ROTATION_LIMIT を超えるとローテーションされる。"""
        state_file = tmp_path / "big_sess.txt"
        # 制限超えのファイルを作成
        lines = [str(i) for i in range(3000)]
        state_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            with patch.object(inject_mod, "DEDUP_ROTATION_LIMIT", 2000):
                result = _load_injected_ids("big_sess")
        # 後半（新しい半分）が残る
        assert 2999 in result
        # 先頭付近は削除される
        assert 0 not in result

    def test_non_digit_lines_ignored(self, tmp_path):
        """数字以外の行は無視される。"""
        state_file = tmp_path / "dirty_sess.txt"
        state_file.write_text("1\nnot_a_number\n2\n\n3\n", encoding="utf-8")

        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            result = _load_injected_ids("dirty_sess")
        assert result == {1, 2, 3}


class TestUpdateInjectionTracking:
    """injected_count / last_injected の更新テスト。"""

    def test_increments_count(self, in_memory_db):
        """注入時に injected_count がインクリメントされる。"""
        conn = in_memory_db
        conn.execute("""
            INSERT INTO findings (reviewer, finding_summary, severity, file_path, injected_count)
            VALUES ('test', 'test finding', 'warning', '/test.py', 0)
        """)
        conn.commit()
        fid = conn.execute("SELECT id FROM findings").fetchone()[0]

        _update_injection_tracking(conn, [{"id": fid}], "2026-01-01T00:00:00")

        row = conn.execute("SELECT injected_count, last_injected FROM findings WHERE id = ?", (fid,)).fetchone()
        assert row["injected_count"] == 1
        assert row["last_injected"] == "2026-01-01T00:00:00"

    def test_empty_findings_noop(self, in_memory_db):
        """空の findings リストでは何もしない。"""
        _update_injection_tracking(in_memory_db, [], "2026-01-01T00:00:00")
        # エラーが出なければ OK


class TestGetFindings:
    """Phase A / Phase B の findings 取得テスト。"""

    def test_returns_empty_without_session_id(self, sample_findings):
        """session_id が空なら注入をスキップ。"""
        findings, is_fallback, repo_root = get_findings(
            "C:/project/hooks/main.py", "", sample_findings
        )
        assert findings == []
        assert is_fallback is False

    def test_phase_a_file_specific(self, sample_findings, tmp_path):
        """Phase A: ファイル特化で pending + 未 dismiss の findings を返す。"""
        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            with patch.object(inject_mod, "_get_project_root", return_value="C:/project"):
                findings, is_fallback, repo_root = get_findings(
                    "C:/project/hooks/main.py", "test_sess", sample_findings
                )
        # id=2 (high, pending, not dismissed), id=3 (warning, pending, not dismissed) がマッチ
        # id=4 (info) は severity フィルタで除外
        assert len(findings) == 2
        assert is_fallback is False
        severities = {f["severity"] for f in findings}
        assert "info" not in severities

    def test_excludes_dismissed(self, sample_findings, tmp_path):
        """dismissed=1 の findings は除外される。"""
        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            with patch.object(inject_mod, "_get_project_root", return_value="C:/project"):
                findings, _, _ = get_findings(
                    "C:/project/hooks/db.py", "test_sess", sample_findings
                )
        # id=1 (critical, pending, not dismissed) のみ
        # id=5 (fixed) は resolution フィルタで除外
        # id=6 (dismissed=1) は dismissed フィルタで除外
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"

    def test_excludes_resolved_via_not_exists(self, sample_findings, tmp_path):
        """resolution='fixed' の findings と同じ file_path+category+summary の finding は除外される。"""
        conn = sample_findings
        # id=5 と同じ file_path+category+summary で pending な finding を追加
        conn.execute("""
            INSERT INTO findings (session_id, repo_root, reviewer, finding_summary, severity, category, file_path, resolution, dismissed)
            VALUES ('sess1', 'C:/project', 'ifr', 'Already fixed', 'high', 'security', 'C:/project/hooks/db.py', 'pending', 0)
        """)
        conn.commit()

        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            with patch.object(inject_mod, "_get_project_root", return_value="C:/project"):
                findings, _, _ = get_findings(
                    "C:/project/hooks/db.py", "test_sess2", conn
                )
        # 'Already fixed' は NOT EXISTS で除外されるべき（id=5 が fixed として存在）
        summaries = [f["finding_summary"] for f in findings]
        assert "Already fixed" not in summaries

    def test_session_dedup(self, sample_findings, tmp_path):
        """同一セッション内で既に注入した ID は再注入されない。"""
        # 事前に ID を注入済みとしてマーク
        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            _save_injected_ids("dedup_sess", {2, 3})  # main.py の findings を既注入にする
            with patch.object(inject_mod, "_get_project_root", return_value="C:/project"):
                findings, _, _ = get_findings(
                    "C:/project/hooks/main.py", "dedup_sess", sample_findings
                )
        assert len(findings) == 0

    def test_phase_b_critical_fallback(self, in_memory_db, tmp_path):
        """Phase B: Phase A が 0 件のとき、プロジェクト横断 critical にフォールバック。"""
        conn = in_memory_db
        # 別ファイルに critical finding を追加
        conn.execute("""
            INSERT INTO findings (session_id, repo_root, reviewer, finding_summary, severity, category, file_path, resolution, dismissed)
            VALUES ('sess1', 'C:/deep/nested/project', 'ifr', 'Critical XSS', 'critical', 'security', 'C:/deep/nested/project/other.py', 'pending', 0)
        """)
        conn.commit()

        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            # _get_project_root が深いパスを返すようにモック
            with patch.object(inject_mod, "_get_project_root", return_value="C:/deep/nested/project"):
                findings, is_fallback, _ = get_findings(
                    "C:/deep/nested/project/new_file.py", "test_sess", conn
                )
        assert len(findings) == 1
        assert is_fallback is True
        assert findings[0]["severity"] == "critical"

    def test_phase_b_skipped_for_shallow_root(self, in_memory_db, tmp_path):
        """Phase B: git root が浅すぎる場合はスキップ。"""
        conn = in_memory_db
        conn.execute("""
            INSERT INTO findings (session_id, repo_root, reviewer, finding_summary, severity, category, file_path, resolution, dismissed)
            VALUES ('sess1', 'C:/', 'ifr', 'Critical bug', 'critical', 'security', 'C:/file.py', 'pending', 0)
        """)
        conn.commit()

        with patch.object(inject_mod, "STATE_DIR", tmp_path):
            with patch.object(inject_mod, "_get_project_root", return_value="C:/"):
                findings, is_fallback, _ = get_findings(
                    "C:/new_file.py", "test_sess", conn
                )
        # 浅い root（C:/ → parts=['C:\\'] = 1 < 4）でスキップ
        assert len(findings) == 0


class TestGetFpPatterns:
    """学習済み FP パターン取得テスト。"""

    def test_returns_patterns_with_2_or_more_dismissals(self, in_memory_db):
        """2回以上 dismiss 承認されたパターンを返す。"""
        conn = in_memory_db
        for _ in range(3):
            conn.execute("""
                INSERT INTO findings (reviewer, finding_summary, severity, category, file_path, resolution,
                    dismissed, dismissed_by, fp_reason, repo_root)
                VALUES ('ifr', 'False alarm', 'warning', 'security', '/test.py', 'pending',
                    1, 'user', 'テスト用の誤検知', 'C:/project')
            """)
        conn.commit()

        patterns = get_fp_patterns(conn, "C:/project")
        assert len(patterns) == 1
        assert patterns[0]["cnt"] == 3
        assert patterns[0]["category"] == "security"

    def test_excludes_critical(self, in_memory_db):
        """severity=critical の dismissed は学習対象外。"""
        conn = in_memory_db
        for _ in range(3):
            conn.execute("""
                INSERT INTO findings (reviewer, finding_summary, severity, category, file_path, resolution,
                    dismissed, dismissed_by, fp_reason, repo_root)
                VALUES ('ifr', 'Crit FP', 'critical', 'security', '/test.py', 'pending',
                    1, 'user', 'critical but FP', 'C:/project')
            """)
        conn.commit()

        patterns = get_fp_patterns(conn, "C:/project")
        assert len(patterns) == 0

    def test_excludes_single_dismissal(self, in_memory_db):
        """1回だけの dismissal は学習対象外（HAVING cnt >= 2）。"""
        conn = in_memory_db
        conn.execute("""
            INSERT INTO findings (reviewer, finding_summary, severity, category, file_path, resolution,
                dismissed, dismissed_by, fp_reason, repo_root)
            VALUES ('ifr', 'One-off FP', 'warning', 'style', '/test.py', 'pending',
                1, 'user', 'single', 'C:/project')
        """)
        conn.commit()

        patterns = get_fp_patterns(conn, "C:/project")
        assert len(patterns) == 0


class TestFormatInjection:
    """注入テキスト生成テスト。"""

    def test_phase_a_header(self):
        """Phase A の場合、ファイル特化ヘッダーを出力する。"""
        findings = [{"id": 1, "severity": "warning", "category": "style", "finding_summary": "Bad naming"}]
        text = format_injection("/test.py", findings, is_fallback=False)
        assert "PAST FINDINGS: /test.py" in text
        assert "PROJECT-WIDE" not in text

    def test_phase_b_header(self):
        """Phase B の場合、プロジェクト横断ヘッダーを出力する。"""
        findings = [{"id": 1, "severity": "critical", "category": "security", "finding_summary": "XSS"}]
        text = format_injection("/new_file.py", findings, is_fallback=True)
        assert "PROJECT-WIDE CRITICAL PATTERNS" in text

    def test_finding_format_includes_id(self):
        """各 finding に ID が表示される（dismiss ディスカバラビリティ）。"""
        findings = [{"id": 42, "severity": "high", "category": "bug", "finding_summary": "Off-by-one"}]
        text = format_injection("/test.py", findings)
        assert "#42" in text

    def test_dismiss_command_shown(self):
        """dismiss コマンドのワンライナーが表示される（一括 dismiss 形式）。"""
        findings = [{"id": 10, "severity": "warning", "category": "style", "finding_summary": "x"}]
        text = format_injection("/test.py", findings)
        assert "review-feedback.py" in text
        assert "dismiss" in text
        assert "--ids 10" in text
        assert "--no-interactive" in text

    def test_fp_patterns_section(self):
        """FP パターンセクションが表示される。"""
        findings = [{"id": 1, "severity": "warning", "category": "style", "finding_summary": "x"}]
        fp = [{"category": "security", "fp_reason": "テスト用", "cnt": 3}]
        text = format_injection("/test.py", findings, fp_patterns=fp)
        assert "学習済みパターン" in text
        assert "テスト用" in text
        assert "3回却下" in text

    def test_no_fp_patterns_when_none(self):
        """FP パターンが None なら表示されない。"""
        findings = [{"id": 1, "severity": "warning", "category": "style", "finding_summary": "x"}]
        text = format_injection("/test.py", findings, fp_patterns=None)
        assert "学習済みパターン" not in text

    def test_count_shown(self):
        """findings 件数が表示される。"""
        findings = [
            {"id": 1, "severity": "warning", "category": "a", "finding_summary": "x"},
            {"id": 2, "severity": "high", "category": "b", "finding_summary": "y"},
        ]
        text = format_injection("/test.py", findings)
        assert "2 件を表示" in text
