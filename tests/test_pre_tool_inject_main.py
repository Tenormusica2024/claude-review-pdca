"""
pre-tool-inject-findings.py の main() 統合テスト。
stdin モック + sys.exit キャッチで main() のエントリポイントを検証する。
"""
import json
import importlib
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

inject_mod = importlib.import_module("pre-tool-inject-findings")


class TestMainEntryPoint:
    """main() のエントリポイントテスト。"""

    def test_non_edit_tool_exits_silently(self):
        """Edit/Write/MultiEdit 以外のツールは即終了。"""
        payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/test.py"}})
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = payload
            with pytest.raises(SystemExit) as exc_info:
                inject_mod.main()
        assert exc_info.value.code == 0

    def test_invalid_json_exits_silently(self):
        """不正な JSON は即終了。"""
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "not json"
            with pytest.raises(SystemExit) as exc_info:
                inject_mod.main()
        assert exc_info.value.code == 0

    def test_no_file_path_exits_silently(self):
        """file_path がないペイロードは即終了。"""
        payload = json.dumps({"tool_name": "Edit", "tool_input": {}})
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = payload
            with pytest.raises(SystemExit) as exc_info:
                inject_mod.main()
        assert exc_info.value.code == 0

    def test_missing_db_exits_silently(self, tmp_path):
        """DB が存在しない場合は即終了。"""
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/test.py"},
            "session_id": "test_sess",
        })
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = payload
            with patch.object(inject_mod, "DB_PATH", tmp_path / "nonexistent.db"):
                with pytest.raises(SystemExit) as exc_info:
                    inject_mod.main()
        assert exc_info.value.code == 0

    def test_edit_with_findings_prints_injection(self, sample_findings, tmp_path, capsys):
        """findings がある場合、注入テキストが stdout に出力される。"""
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": "C:/project/hooks/main.py"},
            "session_id": "integration_sess",
        })

        # DB をファイルに書き出す（:memory: は別プロセスから見えないため）
        import sqlite3
        db_path = tmp_path / "test.db"
        file_conn = sqlite3.connect(str(db_path))
        # sample_findings の内容をコピー
        sample_findings.backup(file_conn)
        file_conn.close()

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = payload
            with patch.object(inject_mod, "DB_PATH", db_path):
                with patch.object(inject_mod, "STATE_DIR", tmp_path / "state"):
                    with patch.object(inject_mod, "_get_project_root", return_value="C:/project"):
                        with pytest.raises(SystemExit) as exc_info:
                            inject_mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "PAST FINDINGS" in captured.out

    def test_multi_edit_collects_all_files(self):
        """MultiEdit は全ファイルを収集する。"""
        payload = json.dumps({
            "tool_name": "MultiEdit",
            "tool_input": {
                "edits": [
                    {"file_path": "C:\\a.py"},
                    {"file_path": "C:\\b.py"},
                ]
            },
            "session_id": "test_sess",
        })
        # DB がないので即終了するが、ファイルパス収集ロジックは通る
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = payload
            with patch.object(inject_mod, "DB_PATH", Path("/nonexistent.db")):
                with pytest.raises(SystemExit) as exc_info:
                    inject_mod.main()
        assert exc_info.value.code == 0

    def test_learned_patterns_are_injected_once_per_session(self, sample_findings, tmp_path, capsys):
        """learned patterns は同一 session で毎回再注入しない。"""
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": "C:/project/hooks/main.py"},
            "session_id": "integration_sess",
        })

        import sqlite3
        db_path = tmp_path / "test.db"
        file_conn = sqlite3.connect(str(db_path))
        sample_findings.backup(file_conn)
        file_conn.close()

        state_dir = tmp_path / "state"

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = payload
            with patch.object(inject_mod, "DB_PATH", db_path):
                with patch.object(inject_mod, "STATE_DIR", state_dir):
                    with patch.object(inject_mod, "_get_project_root", return_value="C:/project"):
                        with patch.object(inject_mod, "get_patterns_for_file", return_value=[{"category": "logic", "pattern_text": "off-by-one", "severity": "warning", "count": 2}]):
                            with patch.object(inject_mod, "format_learned_patterns", return_value="[LEARNED PATTERNS]\n- off-by-one"):
                                with patch.object(inject_mod, "_should_inject_learned_patterns", return_value=True):
                                    with pytest.raises(SystemExit):
                                        inject_mod.main()

        first = capsys.readouterr()
        assert "[LEARNED PATTERNS]" in first.out

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = payload
            with patch.object(inject_mod, "DB_PATH", db_path):
                with patch.object(inject_mod, "STATE_DIR", state_dir):
                    with patch.object(inject_mod, "_get_project_root", return_value="C:/project"):
                        with patch.object(inject_mod, "get_patterns_for_file", return_value=[{"category": "logic", "pattern_text": "off-by-one", "severity": "warning", "count": 2}]):
                            with patch.object(inject_mod, "format_learned_patterns", return_value="[LEARNED PATTERNS]\n- off-by-one"):
                                with patch.object(inject_mod, "_should_inject_learned_patterns", return_value=True):
                                    with pytest.raises(SystemExit):
                                        inject_mod.main()

        second = capsys.readouterr()
        assert "[LEARNED PATTERNS]" not in second.out

    def test_learned_patterns_are_skipped_without_implementation_gate(self, sample_findings, tmp_path, capsys):
        """implementation gate が false の場合は learned patterns を注入しない。"""
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": "C:/project/hooks/main.py"},
            "session_id": "integration_sess",
        })

        import sqlite3
        db_path = tmp_path / "test.db"
        file_conn = sqlite3.connect(str(db_path))
        sample_findings.backup(file_conn)
        file_conn.close()

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = payload
            with patch.object(inject_mod, "DB_PATH", db_path):
                with patch.object(inject_mod, "STATE_DIR", tmp_path / "state"):
                    with patch.object(inject_mod, "_get_project_root", return_value="C:/project"):
                        with patch.object(inject_mod, "get_patterns_for_file", return_value=[{"category": "logic", "pattern_text": "off-by-one", "severity": "warning", "count": 2}]):
                            with patch.object(inject_mod, "format_learned_patterns", return_value="[LEARNED PATTERNS]\n- off-by-one"):
                                with patch.object(inject_mod, "_should_inject_learned_patterns", return_value=False):
                                    with pytest.raises(SystemExit):
                                        inject_mod.main()

        captured = capsys.readouterr()
        assert "[LEARNED PATTERNS]" not in captured.out
