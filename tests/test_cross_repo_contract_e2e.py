"""
review-fix-pipeline → claude-review-pdca の cross-repo contract 受け入れテスト。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch
import pytest


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PDCA_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_ROOT = PDCA_ROOT.parent / "review-fix-pipeline"
PIPELINE_AVAILABLE = PIPELINE_ROOT.exists()

producer_mod = _load_module(PDCA_ROOT / "scripts" / "record-review-outcome.py", "pdca_record_review_outcome")
if PIPELINE_AVAILABLE:
    contract_mod = _load_module(PIPELINE_ROOT / "scripts" / "review_outcome_contract.py", "pipeline_review_outcome_contract")
    output_bridge_mod = _load_module(PIPELINE_ROOT / "scripts" / "review_output_bridge.py", "pipeline_review_output_bridge")
    feedback_bridge_mod = _load_module(PIPELINE_ROOT / "scripts" / "review_feedback_bridge.py", "pipeline_review_feedback_bridge")
else:
    contract_mod = None
    output_bridge_mod = None
    feedback_bridge_mod = None

pytestmark = pytest.mark.skipif(
    not PIPELINE_AVAILABLE,
    reason="review-fix-pipeline sibling repo is not available in this checkout",
)


class TestCrossRepoContractE2E:
    def test_contract_builder_payload_is_accepted_by_pdca_producer(self, capsys):
        payload = contract_mod.build_payload(
            reviewer="sc-ifr",
            repo_root="C:/repo",
            session_id="sess-1",
            runtime="codex",
            mode="review-only",
            target_files=["C:/repo/src/app.py"],
            verification_commands=["pytest -q"],
            verification_summary="ok",
            items=[
                {
                    "type": "finding",
                    "summary": "pending issue",
                    "severity": "warning",
                    "category": "logic",
                    "file_path": "C:/repo/src/app.py",
                    "status": "pending",
                    "confidence": "high",
                    "needs_judgment": True,
                },
                {
                    "type": "finding",
                    "summary": "fixed issue",
                    "severity": "warning",
                    "category": "robustness",
                    "file_path": "C:/repo/src/util.py",
                    "status": "fixed",
                    "confidence": "high",
                },
            ],
        )
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout="ok", stderr="")

        with patch.object(producer_mod, "run_review_feedback_record", return_value=ok) as mock_feedback:
            with patch.object(producer_mod, "run_pattern_record", return_value=ok) as mock_pattern:
                with patch(
                    "sys.argv",
                    [
                        "record-review-outcome.py",
                        "--payload-json",
                        json.dumps(payload, ensure_ascii=False),
                    ],
                ):
                    rc = producer_mod.main()

        captured = capsys.readouterr()
        assert rc == 0
        summary = json.loads(captured.out)
        assert summary["recorded_feedback"] == 1
        assert summary["recorded_patterns"] == 1
        assert mock_feedback.call_args.kwargs["reviewer"] == "intent-first-review"
        assert mock_pattern.call_args.kwargs["reviewer"] == "intent-first-review"

    def test_markdown_bridge_payload_is_accepted_by_pdca_producer(self, capsys):
        markdown = """
## 自動修正可
### quoted shell invocation
- severity: warning
- auto_fixable: true
- 何が起きるか: shell quoted subprocess is fragile
- 対策案:
  - 対象: hooks/review-feedback-session-check.js:42
  - 変更内容: use execFileSync

## 要確認
severity: warning
auto_fixable: false
問題: command contract mismatch
詳細: producer と skill の契約をそろえる必要がある
判断ポイント: machine block を必須にするか
─────────────────────────────
"""
        payload = output_bridge_mod.build_payload(
            reviewer="sc-rfl",
            items=output_bridge_mod.parse_review_output(markdown, auto_fix_status="fixed"),
            repo_root="C:/repo",
            runtime="claude-code",
            mode="normal",
            target_files=["hooks/review-feedback-session-check.js"],
            verification_commands=["pytest -q"],
            verification_summary="ok",
        )
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout="ok", stderr="")

        with patch.object(producer_mod, "run_review_feedback_record", return_value=ok) as mock_feedback:
            with patch.object(producer_mod, "run_pattern_record", return_value=ok) as mock_pattern:
                with patch(
                    "sys.argv",
                    [
                        "record-review-outcome.py",
                        "--payload-json",
                        json.dumps(payload, ensure_ascii=False),
                    ],
                ):
                    rc = producer_mod.main()

        captured = capsys.readouterr()
        assert rc == 0
        summary = json.loads(captured.out)
        assert summary["recorded_feedback"] == 0
        assert summary["recorded_patterns"] == 1
        pattern_findings = mock_pattern.call_args.args[0]
        assert pattern_findings[0]["summary"] == "shell quoted subprocess is fragile"
        assert mock_feedback.call_args is None

    def test_review_feedback_bridge_payload_is_accepted_by_pdca_producer(self, capsys):
        items = feedback_bridge_mod.build_items(
            [
                {
                    "summary": "quoted shell invocation",
                    "severity": "warning",
                    "category": "robustness",
                    "file_path": "hooks/review-feedback-session-check.js",
                    "line": 42,
                }
            ],
            status="fixed",
            confidence="high",
            needs_judgment=False,
        )
        payload = feedback_bridge_mod.build_payload(
            reviewer="review-fix-loop",
            items=items,
            repo_root="C:/repo",
            runtime="claude-code",
            mode="normal",
            target_files=["hooks/review-feedback-session-check.js"],
            verification_summary="ok",
        )
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout="ok", stderr="")

        with patch.object(producer_mod, "run_review_feedback_record", return_value=ok) as mock_feedback:
            with patch.object(producer_mod, "run_pattern_record", return_value=ok) as mock_pattern:
                with patch(
                    "sys.argv",
                    [
                        "record-review-outcome.py",
                        "--payload-json",
                        json.dumps(payload, ensure_ascii=False),
                    ],
                ):
                    rc = producer_mod.main()

        captured = capsys.readouterr()
        assert rc == 0
        summary = json.loads(captured.out)
        assert summary["recorded_feedback"] == 0
        assert summary["recorded_patterns"] == 1
        assert mock_feedback.call_args is None
        assert mock_pattern.call_args.args[0][0]["file_path"] == "hooks/review-feedback-session-check.js"
