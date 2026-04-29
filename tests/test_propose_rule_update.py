"""
propose-rule-update.py のテスト。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch


def _load_module():
    target = Path(__file__).resolve().parent.parent / "scripts" / "propose-rule-update.py"
    spec = importlib.util.spec_from_file_location("propose_rule_update_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proposal_mod = _load_module()


def write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestProposeRuleUpdate:
    def test_create_add_proposal_with_diff(self, tmp_path):
        target = write(tmp_path / "CLAUDE.md", "# Rules\n\n- Existing rule\n")

        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Do not record judgment-required findings as learned patterns before HITL approval.",
            "Prevents unresolved judgment calls from contaminating future implementation memory.",
        )

        assert proposal.status == "proposal-ready"
        assert proposal.action == "add"
        assert proposal.target == str(target.resolve())
        assert "Proposed Review PDCA Rules" in proposal.diff
        assert "+- Do not record judgment-required" in proposal.diff

    def test_build_updated_content_reuses_existing_proposed_section(self):
        before = "# Rules\n\n## Proposed Review PDCA Rules\n\n- Existing proposed rule.\n"

        after = proposal_mod.build_updated_content(before, "New proposed rule.")

        assert after.count("## Proposed Review PDCA Rules") == 1
        assert after.endswith("- Existing proposed rule.\n- New proposed rule.\n")

    def test_duplicate_rule_is_skip_proposal(self, tmp_path):
        write(
            tmp_path / "CLAUDE.md",
            "# Rules\n\n- Do not record judgment-required findings as learned patterns before HITL approval.\n",
        )

        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Do not record judgment-required findings as learned patterns before HITL approval.",
            "Same rule should not be appended twice.",
        )

        assert proposal.status == "duplicate-suspected"
        assert proposal.action == "skip"
        assert proposal.diff == ""
        assert proposal.duplicates[0].line_number == 3

    def test_no_rule_doc_stays_proposal_only(self, tmp_path):
        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Keep highly local wording comments out of project rules.",
            "Avoids bloating rule docs with one-off preferences.",
        )

        assert proposal.status == "proposal-only"
        assert proposal.action == "proposal-only"
        assert proposal.target is None
        assert proposal.diff == ""

    def test_multiple_unclear_docs_stays_proposal_only(self, tmp_path):
        write(tmp_path / "CLAUDE.md", "# Claude\n")
        write(tmp_path / "CODEX.md", "# Codex\n")

        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Prefer proposal-only when canonical rule target is unclear.",
            "Avoids writing to the wrong agent rule file.",
        )

        assert proposal.status == "proposal-only"
        assert proposal.action == "proposal-only"
        assert "Multiple active rule documents" in proposal.resolver_reason

    def test_markdown_format_contains_hitl_block(self, tmp_path):
        write(tmp_path / "CLAUDE.md", "# Rules\n")
        proposal = proposal_mod.create_proposal(tmp_path, "Use resolver before rule writes.", "Target safety.")

        markdown = proposal_mod.format_markdown(proposal)

        assert "## Rule promotion proposal" in markdown
        assert "Approve? yes/no" in markdown
        assert "```diff" in markdown

    def test_json_cli_outputs_machine_readable_proposal(self, tmp_path, capsys):
        write(tmp_path / "CLAUDE.md", "# Rules\n")

        with patch("sys.argv", [
            "propose-rule-update.py",
            "--repo-root", str(tmp_path),
            "--rule", "Use resolver before rule writes.",
            "--adoption-reason", "Target safety.",
            "--json",
        ]):
            assert proposal_mod.main() == 0

        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "proposal-ready"
        assert data["action"] == "add"
        assert data["duplicates"] == []

    def test_log_proposal_records_proposal_only_decision(self, tmp_path):
        write(tmp_path / "CLAUDE.md", "# Rules\n")
        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Use resolver before rule writes.",
            "Target safety.",
        )
        log_path = tmp_path / "proposal-log.jsonl"

        written = proposal_mod.log_proposal(
            proposal,
            tmp_path,
            source="review-outcome",
            log_path=log_path,
        )

        assert written == log_path
        data = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert data["decision"] == "proposal-only"
        assert data["proposal_status"] == "proposal-ready"
        assert data["candidate_summary"] == "Use resolver before rule writes."

    def test_log_duplicate_proposal_records_rejection_reason(self, tmp_path):
        write(tmp_path / "CLAUDE.md", "# Rules\n\n- Use resolver before rule writes.\n")
        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Use resolver before rule writes.",
            "Target safety.",
        )
        log_path = tmp_path / "proposal-log.jsonl"

        proposal_mod.log_proposal(proposal, tmp_path, source="manual", log_path=log_path)

        data = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert data["decision"] == "rejected"
        assert data["proposal_status"] == "duplicate-suspected"
        assert "Likely duplicate" in data["rejection_reason"]
        assert data["existing_rule_refs"]

    def test_cli_can_log_proposal(self, tmp_path, capsys):
        write(tmp_path / "CLAUDE.md", "# Rules\n")
        log_path = tmp_path / "cli-proposal-log.jsonl"

        with patch("sys.argv", [
            "propose-rule-update.py",
            "--repo-root", str(tmp_path),
            "--rule", "Use resolver before rule writes.",
            "--adoption-reason", "Target safety.",
            "--source", "user-correction",
            "--log-proposal",
            "--log-path", str(log_path),
            "--json",
        ]):
            assert proposal_mod.main() == 0

        stdout = capsys.readouterr().out
        payload = json.loads(stdout)
        assert payload["status"] == "proposal-ready"
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["source"] == "user-correction"
        assert entry["proposal_status"] == "proposal-ready"

    def test_apply_requires_user_approval(self, tmp_path):
        target = write(tmp_path / "CLAUDE.md", "# Rules\n")
        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Use resolver before rule writes.",
            "Target safety.",
        )

        result = proposal_mod.apply_proposal(
            proposal,
            tmp_path,
            approved_by_user=False,
            source="manual",
            log_path=tmp_path / "apply-log.jsonl",
        )

        assert result.applied is False
        assert "approved-by-user" in result.reason
        assert target.read_text(encoding="utf-8") == "# Rules\n"

    def test_apply_blocks_duplicate_suspected(self, tmp_path):
        target = write(tmp_path / "CLAUDE.md", "# Rules\n\n- Use resolver before rule writes.\n")
        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Use resolver before rule writes.",
            "Target safety.",
        )

        result = proposal_mod.apply_proposal(
            proposal,
            tmp_path,
            approved_by_user=True,
            source="manual",
            log_path=tmp_path / "apply-log.jsonl",
        )

        assert result.applied is False
        assert "duplicate-suspected" in result.reason
        assert target.read_text(encoding="utf-8") == "# Rules\n\n- Use resolver before rule writes.\n"

    def test_apply_writes_rule_and_adoption_log(self, tmp_path):
        target = write(tmp_path / "CLAUDE.md", "# Rules\n")
        log_path = tmp_path / "apply-log.jsonl"
        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Use resolver before rule writes.",
            "Prevents writing to the wrong rule document.",
        )

        result = proposal_mod.apply_proposal(
            proposal,
            tmp_path,
            approved_by_user=True,
            source="user-correction",
            log_path=log_path,
        )

        assert result.applied is True
        assert "Proposed Review PDCA Rules" in target.read_text(encoding="utf-8")
        assert "- Use resolver before rule writes." in target.read_text(encoding="utf-8")
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["decision"] == "adopted"
        assert entry["user_approved"] is True
        assert entry["adoption_reason"] == "Prevents writing to the wrong rule document."

    def test_apply_rechecks_duplicate_against_current_file(self, tmp_path):
        target = write(tmp_path / "CLAUDE.md", "# Rules\n")
        proposal = proposal_mod.create_proposal(
            tmp_path,
            "Use resolver before rule writes.",
            "Prevents writing to the wrong rule document.",
        )
        target.write_text("# Rules\n\n- Use resolver before rule writes.\n", encoding="utf-8")

        result = proposal_mod.apply_proposal(
            proposal,
            tmp_path,
            approved_by_user=True,
            source="manual",
            log_path=tmp_path / "apply-log.jsonl",
        )

        assert result.applied is False
        assert "likely duplicate" in result.reason

    def test_cli_apply_requires_explicit_approval_flag(self, tmp_path, capsys):
        target = write(tmp_path / "CLAUDE.md", "# Rules\n")
        log_path = tmp_path / "cli-apply-log.jsonl"

        with patch("sys.argv", [
            "propose-rule-update.py",
            "--repo-root", str(tmp_path),
            "--rule", "Use resolver before rule writes.",
            "--adoption-reason", "Target safety.",
            "--apply",
            "--log-path", str(log_path),
            "--json",
        ]):
            assert proposal_mod.main() == 0

        data = json.loads(capsys.readouterr().out)
        assert data["apply_result"]["applied"] is False
        assert target.read_text(encoding="utf-8") == "# Rules\n"

    def test_cli_apply_with_approval_writes_and_logs(self, tmp_path, capsys):
        target = write(tmp_path / "CLAUDE.md", "# Rules\n")
        log_path = tmp_path / "cli-apply-log.jsonl"

        with patch("sys.argv", [
            "propose-rule-update.py",
            "--repo-root", str(tmp_path),
            "--rule", "Use resolver before rule writes.",
            "--adoption-reason", "Target safety.",
            "--apply",
            "--approved-by-user",
            "--log-path", str(log_path),
            "--json",
        ]):
            assert proposal_mod.main() == 0

        data = json.loads(capsys.readouterr().out)
        assert data["apply_result"]["applied"] is True
        assert "- Use resolver before rule writes." in target.read_text(encoding="utf-8")
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["decision"] == "adopted"
