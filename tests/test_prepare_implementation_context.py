"""
prepare-implementation-context.py のテスト。
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch


def _load_module():
    target = Path(__file__).resolve().parent.parent / "scripts" / "prepare-implementation-context.py"
    spec = importlib.util.spec_from_file_location("prepare_implementation_context_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


prepare_mod = _load_module()


class TestPrepareImplementationContext:
    def test_detect_implementation_markers(self):
        markers = prepare_mod.detect_implementation_markers("sc-rfl で /rfl 相当の修正をする")
        assert "sc-rfl" in markers
        assert "/rfl" in markers

    def test_build_hook_payload_for_multiedit(self):
        payload = prepare_mod.build_hook_payload(
            session_id="sess-1",
            cwd="C:/repo",
            tool_name="Edit",
            file_paths=["src/a.py", "src/b.py"],
        )
        assert payload["tool_name"] == "MultiEdit"
        assert payload["tool_input"]["edits"][0]["file_path"] == "src/a.py"
        assert payload["tool_input"]["edits"][1]["file_path"] == "src/b.py"

    def test_main_activates_gate_and_prints_context(self, tmp_path, capsys):
        gate_file = tmp_path / "implementation-session.json"
        completed = subprocess.CompletedProcess(
            args=["python", "hook"],
            returncode=0,
            stdout="=== PAST FINDINGS: src/app/main.py ===\n",
            stderr="",
        )

        with patch.object(prepare_mod, "IMPLEMENTATION_SESSION_PATH", gate_file):
            with patch.object(prepare_mod, "run_pretool_injection", return_value=completed) as mock_run:
                with patch(
                    "sys.argv",
                    [
                        "prepare-implementation-context.py",
                        "--session-id", "sess-impl",
                        "--cwd", str(tmp_path),
                        "--prompt", "sc-rfl で修正する",
                        "--file-path", "src/app/main.py",
                    ],
                ):
                    rc = prepare_mod.main()

        captured = capsys.readouterr()
        assert rc == 0
        assert "implementation gate activated" in captured.err
        assert "=== PAST FINDINGS: src/app/main.py ===" in captured.out
        assert gate_file.exists()
        payload = mock_run.call_args.args[0]
        assert payload["session_id"] == "sess-impl"
        assert payload["tool_name"] == "Edit"
