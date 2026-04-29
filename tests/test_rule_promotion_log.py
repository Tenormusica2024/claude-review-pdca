"""
rule_promotion_log.py のテスト。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rule_promotion_log as log_mod


class TestRulePromotionLog:
    def test_build_adopted_entry_requires_adoption_reason(self, tmp_path):
        with pytest.raises(ValueError, match="adoption_reason"):
            log_mod.build_entry(
                repo_root=tmp_path,
                target_doc="CLAUDE.md",
                source="manual",
                candidate_summary="candidate",
                decision="adopted",
            )

    def test_build_rejected_entry_requires_rejection_reason(self, tmp_path):
        with pytest.raises(ValueError, match="rejection_reason"):
            log_mod.build_entry(
                repo_root=tmp_path,
                target_doc="CLAUDE.md",
                source="manual",
                candidate_summary="candidate",
                decision="rejected",
            )

    def test_append_and_load_entries(self, tmp_path):
        entry = log_mod.build_entry(
            repo_root=tmp_path,
            target_doc=str(tmp_path / "CLAUDE.md"),
            source="user-correction",
            candidate_summary="Keep local wording comments out of project rules.",
            decision="rejected",
            rejection_reason="Too local to reuse across future work.",
            existing_rule_refs=["CLAUDE.md:L10"],
            user_approved=False,
            proposal_status="duplicate-suspected",
            timestamp="2026-04-30T00:00:00+00:00",
        )

        path = log_mod.append_entry(entry, tmp_path / "audit.jsonl")
        entries = log_mod.load_entries(path)

        assert len(entries) == 1
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["rejection_reason"] == "Too local to reuse across future work."
        assert entries[0]["existing_rule_refs"] == ["CLAUDE.md:L10"]

    def test_default_log_path_is_repo_local(self, tmp_path):
        assert log_mod.default_log_path(tmp_path) == tmp_path.resolve() / ".review-pdca-rule-promotions.jsonl"

    def test_cli_appends_entry(self, tmp_path):
        script = Path(__file__).resolve().parent.parent / "scripts" / "rule_promotion_log.py"
        log_path = tmp_path / "cli-log.jsonl"

        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--repo-root",
                str(tmp_path),
                "--target-doc",
                str(tmp_path / "CLAUDE.md"),
                "--source",
                "review-outcome",
                "--candidate-summary",
                "Use resolver before rule writes.",
                "--decision",
                "adopted",
                "--adoption-reason",
                "Prevents writing to the wrong rule document.",
                "--existing-rule-ref",
                "CODEX.md:L40",
                "--user-approved",
                "--proposal-status",
                "proposal-ready",
                "--log-path",
                str(log_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        entries = log_mod.load_entries(log_path)
        assert entries[0]["decision"] == "adopted"
        assert entries[0]["user_approved"] is True
        assert entries[0]["adoption_reason"] == "Prevents writing to the wrong rule document."
