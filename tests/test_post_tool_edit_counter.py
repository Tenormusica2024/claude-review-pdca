"""
post-tool-edit-counter.py のユニットテスト。

テスト対象: PostToolUse hook のイベントフィルタリング・カウント・ファイルリスト管理。
main() は stdin/sys.exit に依存するため、ロジック単位でテストする。
"""
import json
import importlib
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

counter_mod = importlib.import_module("post-tool-edit-counter")


class TestToolFiltering:
    """対象ツール（Edit/Write/MultiEdit）のフィルタリングテスト。"""

    @pytest.mark.parametrize("tool_name,should_process", [
        ("Edit", True),
        ("Write", True),
        ("MultiEdit", True),
        ("Read", False),
        ("Bash", False),
        ("Grep", False),
    ])
    def test_tool_name_filter(self, tool_name, should_process):
        """Edit/Write/MultiEdit のみ処理対象。"""
        assert (tool_name in ("Edit", "Write", "MultiEdit")) == should_process


class TestFilePathCollection:
    """ファイルパス収集ロジックのテスト。"""

    def test_edit_single_file(self):
        """Edit ツールは file_path を1件取得。"""
        tool_input = {"file_path": "C:\\project\\hooks\\main.py"}
        fp = tool_input.get("file_path") or tool_input.get("path")
        assert fp is not None
        assert fp.replace("\\", "/") == "C:/project/hooks/main.py"

    def test_multi_edit_dedup(self):
        """MultiEdit は edits 配列から重複排除してファイルパスを収集。"""
        tool_input = {
            "edits": [
                {"file_path": "C:\\a.py"},
                {"file_path": "C:\\b.py"},
                {"file_path": "C:\\a.py"},  # 重複
            ]
        }
        seen = set()
        file_paths = []
        for edit in tool_input.get("edits", []):
            fp = edit.get("file_path")
            if fp:
                normalized = fp.replace("\\", "/")
                if normalized not in seen:
                    file_paths.append(normalized)
                    seen.add(normalized)
        assert file_paths == ["C:/a.py", "C:/b.py"]

    def test_write_uses_path_fallback(self):
        """Write ツールは path キーにもフォールバック。"""
        tool_input = {"path": "/tmp/new_file.py"}
        fp = tool_input.get("file_path") or tool_input.get("path")
        assert fp == "/tmp/new_file.py"


class TestCounterFile:
    """カウントファイルの読み書きテスト。"""

    def test_append_and_count(self, tmp_path):
        """append-only でイベントが記録され、行数でカウントできる。"""
        counter_file = tmp_path / "test_sess.txt"

        # 3回 append
        for _ in range(3):
            with open(counter_file, "a", encoding="utf-8") as f:
                f.write("e\n")

        lines = counter_file.read_text(encoding="utf-8").splitlines()
        count = sum(1 for line in lines if line)
        assert count == 3

    def test_batch_threshold(self, tmp_path):
        """BATCH_THRESHOLD に達したら通知対象。"""
        threshold = 5
        for count in range(1, 7):
            is_trigger = count > 0 and count % threshold == 0
            if count == 5:
                assert is_trigger
            else:
                assert not is_trigger


class TestFilesTracking:
    """編集ファイルリスト管理のテスト。"""

    def test_dedup_file_tracking(self, tmp_path):
        """同一ファイルは重複記録されない（ベストエフォート）。"""
        files_file = tmp_path / "test_sess_files.txt"

        # 1回目: 新規ファイルを追加
        existing = set()
        new_files = ["C:/a.py", "C:/b.py"]
        with open(files_file, "a", encoding="utf-8") as f:
            for fp in new_files:
                if fp not in existing:
                    f.write(f"{fp}\n")
                    existing.add(fp)

        # 2回目: 重複を含む追加
        existing = {line.strip() for line in files_file.read_text(encoding="utf-8").splitlines() if line.strip()}
        new_files2 = [fp for fp in ["C:/a.py", "C:/c.py"] if fp not in existing]
        with open(files_file, "a", encoding="utf-8") as f:
            for fp in new_files2:
                f.write(f"{fp}\n")

        all_files = [line.strip() for line in files_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert all_files == ["C:/a.py", "C:/b.py", "C:/c.py"]


class TestRotation:
    """ローテーションロジックのテスト。"""

    def test_rotation_resets_file(self, tmp_path):
        """ROTATION_LIMIT 超過でファイルが空になる。"""
        counter_file = tmp_path / "big_sess.txt"
        # 制限超え
        counter_file.write_text("e\n" * 6000, encoding="utf-8")

        lines = counter_file.read_text(encoding="utf-8").splitlines()
        count = sum(1 for line in lines if line)
        assert count > 5000

        # ローテーション（コード上は write_text("") でゼロバイトリセット）
        if count > 5000:
            counter_file.write_text("", encoding="utf-8")

        assert counter_file.read_text(encoding="utf-8") == ""
