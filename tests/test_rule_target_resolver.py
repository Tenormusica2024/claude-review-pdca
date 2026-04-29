"""
rule_target_resolver.py のテスト。
"""
from __future__ import annotations

from pathlib import Path

import rule_target_resolver as resolver


def write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestRuleTargetResolver:
    def test_no_rule_doc_is_proposal_only(self, tmp_path):
        result = resolver.resolve_rule_target(tmp_path)

        assert result.status == "proposal-only"
        assert result.target is None
        assert result.candidates == ()
        assert not result.can_write

    def test_single_rule_doc_resolves(self, tmp_path):
        target = write(tmp_path / "CLAUDE.md", "# Rules\n")

        result = resolver.resolve_rule_target(tmp_path)

        assert result.status == "resolved"
        assert result.target == target.resolve()
        assert result.can_write

    def test_multiple_docs_without_canonical_is_proposal_only(self, tmp_path):
        write(tmp_path / "CLAUDE.md", "# Claude rules\nUse hooks.\n")
        write(tmp_path / "CODEX.md", "# Codex rules\nUse manual bridge.\n")

        result = resolver.resolve_rule_target(tmp_path)

        assert result.status == "proposal-only"
        assert result.target is None
        assert {c.name for c in result.candidates} == {"CLAUDE.md", "CODEX.md"}

    def test_multiple_docs_with_explicit_canonical_resolves(self, tmp_path):
        target = write(tmp_path / "CLAUDE.md", "# Claude rules\n")
        write(tmp_path / "AGENTS.md", "Canonical source of truth: CLAUDE.md\n")

        result = resolver.resolve_rule_target(tmp_path)

        assert result.status == "resolved"
        assert result.target == target.resolve()

    def test_pointer_doc_resolves_to_referenced_target(self, tmp_path):
        target = write(tmp_path / "AGENTS.md", "# Agent rules\n")
        write(tmp_path / "CODEX.md", "Read AGENTS.md before editing.\n")

        result = resolver.resolve_rule_target(tmp_path)

        assert result.status == "resolved"
        assert result.target == target.resolve()

    def test_identical_docs_resolve_by_priority(self, tmp_path):
        target = write(tmp_path / "CLAUDE.md", "# Same\n- rule\n")
        write(tmp_path / "AGENTS.md", "# Same\n- rule\n")

        result = resolver.resolve_rule_target(tmp_path)

        assert result.status == "resolved"
        assert result.target == target.resolve()

    def test_case_insensitive_file_lookup(self, tmp_path):
        target = write(tmp_path / "agent.md", "# lower-case agent rules\n")

        result = resolver.resolve_rule_target(tmp_path)

        assert result.status == "resolved"
        assert result.target == target.resolve()
        assert result.candidates[0].name == "agent.md"
