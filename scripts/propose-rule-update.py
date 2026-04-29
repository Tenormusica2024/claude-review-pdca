"""Create rule document update proposals for rule promotion.

By default this command never writes the target rule document. It resolves the
safe target, checks for likely duplicate rules, and prints a compact HITL
proposal with a diff. Writes require both --apply and --approved-by-user.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import rule_target_resolver
import rule_promotion_log


@dataclass(frozen=True)
class DuplicateCandidate:
    line_number: int
    line: str
    score: float


@dataclass(frozen=True)
class RuleProposal:
    status: str  # proposal-ready | duplicate-suspected | proposal-only
    target: str | None
    action: str  # add | skip | proposal-only
    rule: str
    adoption_reason: str
    resolver_reason: str
    duplicates: tuple[DuplicateCandidate, ...]
    diff: str


@dataclass(frozen=True)
class ApplyResult:
    applied: bool
    target: str | None
    reason: str
    log_path: str | None = None


def normalize_rule_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^[-*]\s+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def token_set(text: str) -> set[str]:
    return {token for token in re.split(r"[^0-9A-Za-zぁ-んァ-ヶ一-龠ー_]+", normalize_rule_text(text)) if token}


def similarity(a: str, b: str) -> float:
    a_norm = normalize_rule_text(a)
    b_norm = normalize_rule_text(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm in b_norm or b_norm in a_norm:
        return 1.0
    a_tokens = token_set(a_norm)
    b_tokens = token_set(b_norm)
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens) / len(a_tokens | b_tokens)
    sequence = difflib.SequenceMatcher(None, a_norm, b_norm).ratio()
    return max(overlap, sequence)


def find_duplicate_candidates(content: str, rule: str, *, threshold: float = 0.72) -> tuple[DuplicateCandidate, ...]:
    duplicates: list[DuplicateCandidate] = []
    for idx, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        score = similarity(rule, stripped)
        if score >= threshold:
            duplicates.append(DuplicateCandidate(line_number=idx, line=stripped, score=round(score, 3)))
    return tuple(sorted(duplicates, key=lambda item: item.score, reverse=True)[:5])


def format_rule_line(rule: str) -> str:
    stripped = rule.strip()
    if stripped.startswith(('-', '*')):
        return stripped
    return f"- {stripped}"


def build_updated_content(content: str, rule: str) -> str:
    rule_line = format_rule_line(rule)
    if not content.strip():
        return f"# Project Rules\n\n{rule_line}\n"
    heading = "## Proposed Review PDCA Rules"
    if heading in content:
        separator = "" if content.endswith("\n") else "\n"
        return f"{content}{separator}{rule_line}\n"
    separator = "" if content.endswith("\n") else "\n"
    return f"{content}{separator}\n{heading}\n\n{rule_line}\n"


def build_diff(target: Path, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=str(target),
        tofile=f"{target} (proposed)",
    ))


def proposal_after_content(proposal: RuleProposal) -> str:
    if proposal.status != "proposal-ready" or proposal.action != "add" or not proposal.target:
        raise ValueError("Only proposal-ready add proposals can be applied")
    target = Path(proposal.target)
    before = rule_target_resolver._read_text(target)
    return build_updated_content(before, proposal.rule)


def create_proposal(repo_root: str | Path, rule: str, adoption_reason: str) -> RuleProposal:
    resolution = rule_target_resolver.resolve_rule_target(repo_root)
    if not resolution.can_write or resolution.target is None:
        return RuleProposal(
            status="proposal-only",
            target=str(resolution.target) if resolution.target else None,
            action="proposal-only",
            rule=rule.strip(),
            adoption_reason=adoption_reason.strip(),
            resolver_reason=resolution.reason,
            duplicates=(),
            diff="",
        )

    target = resolution.target
    before = rule_target_resolver._read_text(target)
    duplicates = find_duplicate_candidates(before, rule)
    if duplicates:
        return RuleProposal(
            status="duplicate-suspected",
            target=str(target),
            action="skip",
            rule=rule.strip(),
            adoption_reason=adoption_reason.strip(),
            resolver_reason=resolution.reason,
            duplicates=duplicates,
            diff="",
        )

    after = build_updated_content(before, rule)
    return RuleProposal(
        status="proposal-ready",
        target=str(target),
        action="add",
        rule=rule.strip(),
        adoption_reason=adoption_reason.strip(),
        resolver_reason=resolution.reason,
        duplicates=duplicates,
        diff=build_diff(target, before, after),
    )


def format_markdown(proposal: RuleProposal) -> str:
    lines = [
        "## Rule promotion proposal",
        "",
        f"Status: {proposal.status}",
        f"Target: {proposal.target or '(none)'}",
        f"Action: {proposal.action}",
        "",
        "Proposed rule:",
        f"- {proposal.rule}",
        "",
        "Adoption reason:",
        f"- {proposal.adoption_reason or '(not provided)'}",
        "",
        "Resolver:",
        f"- {proposal.resolver_reason}",
    ]
    if proposal.duplicates:
        lines.extend(["", "Existing-rule candidates:"])
        for dup in proposal.duplicates:
            lines.append(f"- L{dup.line_number} score={dup.score}: {dup.line}")
    if proposal.diff:
        lines.extend(["", "Diff:", "```diff", proposal.diff.rstrip(), "```"])
    lines.extend(["", "Approve? yes/no"])
    return "\n".join(lines) + "\n"


def proposal_to_json(proposal: RuleProposal) -> str:
    data = asdict(proposal)
    data["duplicates"] = [asdict(item) for item in proposal.duplicates]
    return json.dumps(data, ensure_ascii=False, indent=2)


def log_proposal(proposal: RuleProposal, repo_root: str | Path, *, source: str, log_path: str | Path | None = None) -> Path:
    decision = "proposal-only"
    rejection_reason = ""
    if proposal.status == "duplicate-suspected":
        decision = "rejected"
        rejection_reason = "Likely duplicate of existing rule; proposal skipped before HITL approval."

    existing_rule_refs = [
        f"{proposal.target}:L{dup.line_number}: {dup.line}"
        for dup in proposal.duplicates
    ]
    entry = rule_promotion_log.build_entry(
        repo_root=repo_root,
        target_doc=proposal.target,
        source=source,
        candidate_summary=proposal.rule,
        decision=decision,
        adoption_reason="",
        rejection_reason=rejection_reason,
        existing_rule_refs=existing_rule_refs,
        user_approved=False,
        proposal_status=proposal.status,
    )
    return rule_promotion_log.append_entry(entry, log_path or rule_promotion_log.default_log_path(repo_root))


def log_applied_proposal(proposal: RuleProposal, repo_root: str | Path, *, source: str, log_path: str | Path | None = None) -> Path:
    entry = rule_promotion_log.build_entry(
        repo_root=repo_root,
        target_doc=proposal.target,
        source=source,
        candidate_summary=proposal.rule,
        decision="adopted",
        adoption_reason=proposal.adoption_reason,
        rejection_reason="",
        existing_rule_refs=[],
        user_approved=True,
        proposal_status=proposal.status,
    )
    return rule_promotion_log.append_entry(entry, log_path or rule_promotion_log.default_log_path(repo_root))


def apply_proposal(
    proposal: RuleProposal,
    repo_root: str | Path,
    *,
    approved_by_user: bool,
    source: str,
    log_path: str | Path | None = None,
) -> ApplyResult:
    if not approved_by_user:
        return ApplyResult(applied=False, target=proposal.target, reason="Apply blocked: --approved-by-user is required.")
    if proposal.status != "proposal-ready" or proposal.action != "add" or not proposal.target:
        return ApplyResult(applied=False, target=proposal.target, reason=f"Apply blocked: proposal status is {proposal.status}/{proposal.action}.")
    if not proposal.adoption_reason.strip():
        return ApplyResult(applied=False, target=proposal.target, reason="Apply blocked: adoption reason is required.")

    target = Path(proposal.target)
    current_content = rule_target_resolver._read_text(target)
    duplicates = find_duplicate_candidates(current_content, proposal.rule)
    if duplicates:
        return ApplyResult(applied=False, target=proposal.target, reason="Apply blocked: current target already contains a likely duplicate rule.")
    after = proposal_after_content(proposal)
    target.write_text(after, encoding="utf-8", newline="\n")
    written_log = log_applied_proposal(proposal, repo_root, source=source, log_path=log_path)
    return ApplyResult(applied=True, target=str(target), reason="Applied after explicit user approval.", log_path=str(written_log))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a proposal-only rule promotion diff.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--rule", required=True)
    parser.add_argument("--adoption-reason", default="")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--log-proposal", action="store_true")
    parser.add_argument("--log-path")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-by-user", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    proposal = create_proposal(args.repo_root, args.rule, args.adoption_reason)
    logged_path = log_proposal(proposal, args.repo_root, source=args.source, log_path=args.log_path) if args.log_proposal else None
    apply_result = apply_proposal(
        proposal,
        args.repo_root,
        approved_by_user=args.approved_by_user,
        source=args.source,
        log_path=args.log_path,
    ) if args.apply else None
    if logged_path and not args.json:
        print(f"Logged proposal: {logged_path}")
    if apply_result and not args.json:
        print(f"Apply result: {apply_result.reason}")
    if args.json:
        data = json.loads(proposal_to_json(proposal))
        if logged_path:
            data["proposal_log_path"] = str(logged_path)
        if apply_result:
            data["apply_result"] = asdict(apply_result)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(proposal))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
