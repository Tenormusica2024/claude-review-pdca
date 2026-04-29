"""
record-review-outcome.py のテスト。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch


def _load_module():
    target = Path(__file__).resolve().parent.parent / "scripts" / "record-review-outcome.py"
    spec = importlib.util.spec_from_file_location("record_review_outcome_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


producer_mod = _load_module()


class TestRecordReviewOutcome:
    def test_normalize_reviewer_aliases(self):
        assert producer_mod.normalize_reviewer("sc-rfl") == "review-fix-loop"
        assert producer_mod.normalize_reviewer("sc-ifr") == "intent-first-review"
        assert producer_mod.normalize_reviewer("/ifr") == "intent-first-review"
        assert producer_mod.normalize_reviewer("sc-gr") == "go-robust"
        assert producer_mod.normalize_reviewer("sc-ir") == "intent-review-light"

    def test_build_feedback_and_pattern_findings(self):
        items = [
            {
                "type": "finding",
                "summary": "pending warning",
                "severity": "warning",
                "category": "logic",
                "file_path": "src/a.py",
                "status": "pending",
                "confidence": "high",
            },
            {
                "type": "finding",
                "summary": "fixed robust issue",
                "severity": "high",
                "category": "robustness",
                "file_path": "src/b.py",
                "status": "fixed",
                "confidence": "high",
            },
            {
                "type": "finding",
                "summary": "info only",
                "severity": "info",
                "category": "maintainability",
                "file_path": "src/c.py",
                "status": "pending",
                "confidence": "high",
            },
        ]
        normalized = [producer_mod.normalize_item(item, "C:/repo") for item in items]

        feedback = producer_mod.build_feedback_findings(normalized, "review-fix-loop")
        patterns = producer_mod.build_pattern_findings(normalized, "review-fix-loop")

        assert [f["summary"] for f in feedback] == ["pending warning"]
        assert [f["summary"] for f in patterns] == ["fixed robust issue"]

    def test_review_fix_loop_records_only_safe_pending_patterns(self):
        items = [
            {
                "type": "finding",
                "summary": "safe auto-fixable pending issue",
                "severity": "warning",
                "category": "robustness",
                "file_path": "src/a.py",
                "status": "pending",
                "confidence": "high",
                "auto_fixable": True,
                "needs_judgment": False,
            },
            {
                "type": "finding",
                "summary": "pending issue needing judgment",
                "severity": "warning",
                "category": "api-contract",
                "file_path": "src/b.py",
                "status": "pending",
                "confidence": "high",
                "auto_fixable": True,
                "needs_judgment": True,
            },
            {
                "type": "finding",
                "summary": "explicit judgment issue",
                "severity": "warning",
                "category": "logic",
                "file_path": "src/c.py",
                "status": "judgment-required",
                "confidence": "high",
                "auto_fixable": True,
                "needs_judgment": True,
            },
        ]
        normalized = [producer_mod.normalize_item(item, "C:/repo") for item in items]

        patterns = producer_mod.build_pattern_findings(normalized, "review-fix-loop")

        assert [f["summary"] for f in patterns] == ["safe auto-fixable pending issue"]

    def test_sc_ir_is_stricter(self):
        items = [
            {
                "type": "finding",
                "summary": "light pending issue",
                "severity": "warning",
                "category": "logic",
                "file_path": "src/a.py",
                "status": "pending",
                "confidence": "high",
            },
            {
                "type": "finding",
                "summary": "light fixed issue",
                "severity": "warning",
                "category": "logic",
                "file_path": "src/b.py",
                "status": "fixed",
                "confidence": "high",
            },
        ]
        normalized = [producer_mod.normalize_item(item, "C:/repo") for item in items]

        feedback = producer_mod.build_feedback_findings(normalized, "intent-review-light")
        patterns = producer_mod.build_pattern_findings(normalized, "intent-review-light")

        assert [f["summary"] for f in feedback] == ["light pending issue"]
        assert [f["summary"] for f in patterns] == ["light fixed issue"]

    def test_main_routes_payload_and_prints_summary(self, capsys):
        payload = {
            "session_id": "sess-1",
            "repo_root": "C:/repo",
            "reviewer": "sc-ifr",
            "items": [
                {
                    "type": "finding",
                    "summary": "pending issue",
                    "severity": "warning",
                    "category": "logic",
                    "file_path": "src/app.py",
                    "status": "pending",
                    "confidence": "high",
                    "needs_judgment": True,
                },
                {
                    "type": "finding",
                    "summary": "fixed issue",
                    "severity": "warning",
                    "category": "robustness",
                    "file_path": "src/util.py",
                    "status": "fixed",
                    "confidence": "high",
                },
                {
                    "type": "judgment_call",
                    "summary": "business choice",
                    "severity": "info",
                    "file_path": "README.md",
                    "status": "judgment-required",
                    "confidence": "medium",
                    "needs_judgment": True,
                },
            ],
        }
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout="ok", stderr="")

        with patch.object(producer_mod, "run_review_feedback_record", return_value=ok) as mock_feedback:
            with patch.object(producer_mod, "run_pattern_record", return_value=ok) as mock_pattern:
                with patch(
                    "sys.argv",
                    [
                        "record-review-outcome.py",
                        "--payload-json", json.dumps(payload, ensure_ascii=False),
                    ],
                ):
                    rc = producer_mod.main()

        captured = capsys.readouterr()
        assert rc == 0
        summary = json.loads(captured.out)
        assert summary["recorded_feedback"] == 1
        assert summary["recorded_patterns"] == 1
        assert summary["judgment_items"] == 2
        assert summary["ignored_items"] == 1

        feedback_call = mock_feedback.call_args
        feedback_kwargs = feedback_call.kwargs
        feedback_findings = feedback_call.args[0]
        assert feedback_kwargs["reviewer"] == "intent-first-review"
        assert feedback_kwargs["session_id"] == "sess-1"
        assert feedback_kwargs["repo_root"] == "C:/repo"
        assert feedback_findings[0]["summary"] == "pending issue"

        pattern_call = mock_pattern.call_args
        pattern_kwargs = pattern_call.kwargs
        pattern_findings = pattern_call.args[0]
        assert pattern_kwargs["reviewer"] == "intent-first-review"
        assert [f["summary"] for f in pattern_findings] == ["fixed issue"]

    def test_go_robust_records_fixed_patterns_only(self):
        items = [
            {
                "type": "finding",
                "summary": "resolved robustness choice",
                "severity": "warning",
                "category": "robustness",
                "file_path": "src/a.py",
                "status": "fixed",
                "confidence": "high",
            },
            {
                "type": "finding",
                "summary": "still needs business input",
                "severity": "warning",
                "category": "api-contract",
                "file_path": "src/b.py",
                "status": "judgment-required",
                "confidence": "high",
                "needs_judgment": True,
            },
        ]
        normalized = [producer_mod.normalize_item(item, "C:/repo") for item in items]

        feedback = producer_mod.build_feedback_findings(normalized, "go-robust")
        patterns = producer_mod.build_pattern_findings(normalized, "go-robust")

        assert [f["summary"] for f in feedback] == ["still needs business input"]
        assert [f["summary"] for f in patterns] == ["resolved robustness choice"]

    def test_build_rule_candidates_requires_high_confidence_reason_and_no_judgment(self):
        items = [
            {
                "type": "rule_candidate",
                "summary": "Use resolver before rule writes.",
                "adoption_reason": "Prevents writing to the wrong rule document.",
                "confidence": "high",
            },
            {
                "type": "rule_candidate",
                "summary": "Needs user decision first.",
                "adoption_reason": "Business judgment.",
                "confidence": "high",
                "needs_judgment": True,
            },
            {
                "type": "rule_candidate",
                "summary": "No reason.",
                "confidence": "high",
            },
        ]
        normalized = [producer_mod.normalize_item(item, "C:/repo") for item in items]

        candidates = producer_mod.build_rule_candidates(normalized)

        assert candidates == [
            {
                "rule": "Use resolver before rule writes.",
                "adoption_reason": "Prevents writing to the wrong rule document.",
            }
        ]

    def test_main_can_generate_rule_proposals_when_enabled(self, capsys):
        payload = {
            "session_id": "sess-1",
            "repo_root": "C:/repo",
            "reviewer": "sc-rfl",
            "items": [
                {
                    "type": "rule_candidate",
                    "summary": "Use resolver before rule writes.",
                    "adoption_reason": "Prevents writing to the wrong rule document.",
                    "confidence": "high",
                },
                {
                    "type": "rule_candidate",
                    "summary": "Needs judgment.",
                    "adoption_reason": "Business judgment.",
                    "confidence": "high",
                    "needs_judgment": True,
                },
            ],
        }
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout='{"status":"proposal-ready"}', stderr="")

        with patch.object(producer_mod, "run_rule_proposal", return_value=ok) as mock_rule:
            with patch(
                "sys.argv",
                [
                    "record-review-outcome.py",
                    "--payload-json", json.dumps(payload, ensure_ascii=False),
                    "--propose-rules",
                    "--rule-log-path", "C:/repo/rule-log.jsonl",
                ],
            ):
                rc = producer_mod.main()

        captured = capsys.readouterr()
        assert rc == 0
        summary = json.loads(captured.out)
        assert summary["rule_proposals"] == 1
        assert summary["ignored_items"] == 1
        assert summary["rule_proposal_errors"] == []
        call = mock_rule.call_args
        assert call.args[0]["rule"] == "Use resolver before rule writes."
        assert call.kwargs["repo_root"] == "C:/repo"
        assert call.kwargs["reviewer"] == "review-fix-loop"
        assert call.kwargs["log_path"] == "C:/repo/rule-log.jsonl"

    def test_rule_candidates_are_ignored_when_proposals_disabled(self, capsys):
        payload = {
            "repo_root": "C:/repo",
            "reviewer": "sc-rfl",
            "items": [
                {
                    "type": "rule_candidate",
                    "summary": "Use resolver before rule writes.",
                    "adoption_reason": "Prevents writing to the wrong rule document.",
                    "confidence": "high",
                }
            ],
        }

        with patch.object(producer_mod, "run_rule_proposal") as mock_rule:
            with patch(
                "sys.argv",
                [
                    "record-review-outcome.py",
                    "--payload-json", json.dumps(payload, ensure_ascii=False),
                ],
            ):
                rc = producer_mod.main()

        captured = capsys.readouterr()
        assert rc == 0
        summary = json.loads(captured.out)
        assert summary["rule_proposals"] == 0
        assert summary["ignored_items"] == 1
        mock_rule.assert_not_called()

    def test_rule_candidate_without_repo_root_reports_skip_error(self, capsys):
        payload = {
            "reviewer": "sc-rfl",
            "items": [
                {
                    "type": "rule_candidate",
                    "summary": "Use resolver before rule writes.",
                    "adoption_reason": "Prevents writing to the wrong rule document.",
                    "confidence": "high",
                }
            ],
        }

        with patch.object(producer_mod, "detect_repo_root", return_value=None):
            with patch.object(producer_mod, "run_rule_proposal") as mock_rule:
                with patch(
                    "sys.argv",
                    [
                        "record-review-outcome.py",
                        "--payload-json", json.dumps(payload, ensure_ascii=False),
                        "--propose-rules",
                    ],
                ):
                    rc = producer_mod.main()

        captured = capsys.readouterr()
        assert rc == 0
        summary = json.loads(captured.out)
        assert summary["rule_proposals"] == 1
        assert summary["rule_proposal_errors"] == ["rule proposal skipped: repo_root is required"]
        mock_rule.assert_not_called()
