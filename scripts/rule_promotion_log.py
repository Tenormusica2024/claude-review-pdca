"""Append-only audit log for rule promotion adoption/rejection decisions."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_DECISIONS = {"adopted", "rejected", "proposal-only"}
DEFAULT_LOG_NAME = ".review-pdca-rule-promotions.jsonl"


@dataclass(frozen=True)
class RulePromotionLogEntry:
    timestamp: str
    repo_root: str
    target_doc: str | None
    source: str
    candidate_summary: str
    decision: str
    adoption_reason: str
    rejection_reason: str
    existing_rule_refs: tuple[str, ...]
    user_approved: bool
    proposal_status: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_decision(decision: str) -> str:
    normalized = decision.strip().lower()
    if normalized not in VALID_DECISIONS:
        raise ValueError(f"Invalid decision: {decision}. Expected one of {sorted(VALID_DECISIONS)}")
    return normalized


def default_log_path(repo_root: str | Path) -> Path:
    return Path(repo_root).resolve() / DEFAULT_LOG_NAME


def build_entry(
    *,
    repo_root: str | Path,
    target_doc: str | None,
    source: str,
    candidate_summary: str,
    decision: str,
    adoption_reason: str = "",
    rejection_reason: str = "",
    existing_rule_refs: list[str] | tuple[str, ...] | None = None,
    user_approved: bool = False,
    proposal_status: str | None = None,
    timestamp: str | None = None,
) -> RulePromotionLogEntry:
    normalized_decision = normalize_decision(decision)
    if normalized_decision == "adopted" and not adoption_reason.strip():
        raise ValueError("adopted decisions require adoption_reason")
    if normalized_decision == "rejected" and not rejection_reason.strip():
        raise ValueError("rejected decisions require rejection_reason")

    return RulePromotionLogEntry(
        timestamp=timestamp or now_iso(),
        repo_root=str(Path(repo_root).resolve()),
        target_doc=target_doc,
        source=source.strip() or "unknown",
        candidate_summary=candidate_summary.strip(),
        decision=normalized_decision,
        adoption_reason=adoption_reason.strip(),
        rejection_reason=rejection_reason.strip(),
        existing_rule_refs=tuple(existing_rule_refs or ()),
        user_approved=bool(user_approved),
        proposal_status=proposal_status,
    )


def append_entry(entry: RulePromotionLogEntry, log_path: str | Path) -> Path:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = asdict(entry)
    data["existing_rule_refs"] = list(entry.existing_rule_refs)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load_entries(log_path: str | Path) -> list[dict[str, Any]]:
    path = Path(log_path)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a rule promotion decision audit log entry.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--target-doc")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--candidate-summary", required=True)
    parser.add_argument("--decision", required=True, choices=sorted(VALID_DECISIONS))
    parser.add_argument("--adoption-reason", default="")
    parser.add_argument("--rejection-reason", default="")
    parser.add_argument("--existing-rule-ref", action="append", default=[])
    parser.add_argument("--user-approved", action="store_true")
    parser.add_argument("--proposal-status")
    parser.add_argument("--log-path")
    args = parser.parse_args()

    entry = build_entry(
        repo_root=args.repo_root,
        target_doc=args.target_doc,
        source=args.source,
        candidate_summary=args.candidate_summary,
        decision=args.decision,
        adoption_reason=args.adoption_reason,
        rejection_reason=args.rejection_reason,
        existing_rule_refs=args.existing_rule_ref,
        user_approved=args.user_approved,
        proposal_status=args.proposal_status,
    )
    path = append_entry(entry, args.log_path or default_log_path(args.repo_root))
    print(json.dumps({"ok": True, "log_path": str(path), "entry": asdict(entry)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
