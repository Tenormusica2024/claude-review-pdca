"""Resolve the repo-local rule document target for rule promotion.

This module is intentionally conservative: when multiple plausible rule docs exist and
no canonical target is explicit, it returns proposal-only instead of writing.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

RULE_DOC_NAMES = ("CLAUDE.md", "AGENTS.md", "AGENT.md", "CODEX.md")
CANONICAL_KEYWORDS = (
    "canonical",
    "source of truth",
    "master rule",
    "master rules",
    "primary rule",
    "primary rules",
    "正本",
    "マスター",
    "優先",
    "従う",
)
POINTER_KEYWORDS = (
    "see",
    "read",
    "follow",
    "refer",
    "参照",
    "読む",
)


@dataclass(frozen=True)
class RuleDocCandidate:
    name: str
    path: Path
    size: int
    content_hash: str
    preview: str


@dataclass(frozen=True)
class RuleTargetResolution:
    status: str  # resolved | proposal-only
    target: Path | None
    candidates: tuple[RuleDocCandidate, ...]
    reason: str

    @property
    def can_write(self) -> bool:
        return self.status == "resolved" and self.target is not None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _content_digest(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest()


def _candidate_preview(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return ""


def find_rule_docs(repo_root: str | Path) -> tuple[RuleDocCandidate, ...]:
    root = Path(repo_root).resolve()
    if not root.exists() or not root.is_dir():
        return ()

    lower_to_path = {p.name.lower(): p for p in root.iterdir() if p.is_file()}
    candidates: list[RuleDocCandidate] = []
    for name in RULE_DOC_NAMES:
        path = lower_to_path.get(name.lower())
        if not path:
            continue
        text = _read_text(path)
        candidates.append(
            RuleDocCandidate(
                name=path.name,
                path=path,
                size=path.stat().st_size,
                content_hash=_content_digest(text),
                preview=_candidate_preview(text),
            )
        )
    return tuple(candidates)


def _line_mentions_canonical_target(line: str, candidate_names: set[str]) -> str | None:
    lower = line.lower()
    if not any(keyword in lower for keyword in CANONICAL_KEYWORDS):
        return None
    for name in candidate_names:
        if name.lower() in lower:
            return name
    return None


def _line_points_to_target(line: str, candidate_names: set[str]) -> str | None:
    lower = line.lower()
    if not any(keyword in lower for keyword in POINTER_KEYWORDS):
        return None
    for name in candidate_names:
        if name.lower() in lower:
            return name
    return None


def detect_canonical_target(candidates: tuple[RuleDocCandidate, ...]) -> Path | None:
    if not candidates:
        return None

    by_name = {candidate.name: candidate for candidate in candidates}
    names = set(by_name)
    strong_refs: list[str] = []
    weak_refs: list[str] = []

    for candidate in candidates:
        text = _read_text(candidate.path)
        for line in text.splitlines():
            strong = _line_mentions_canonical_target(line, names)
            if strong:
                strong_refs.append(strong)
                continue
            weak = _line_points_to_target(line, names)
            if weak and weak != candidate.name:
                weak_refs.append(weak)

    unique_strong = set(strong_refs)
    if len(unique_strong) == 1:
        return by_name[next(iter(unique_strong))].path

    unique_weak = set(weak_refs)
    if len(unique_weak) == 1:
        return by_name[next(iter(unique_weak))].path

    normalized_hashes = {candidate.content_hash for candidate in candidates}
    if len(normalized_hashes) == 1:
        # Identical docs are safe to resolve to the first priority entry.
        return candidates[0].path

    return None


def resolve_rule_target(repo_root: str | Path) -> RuleTargetResolution:
    candidates = find_rule_docs(repo_root)
    if not candidates:
        return RuleTargetResolution(
            status="proposal-only",
            target=None,
            candidates=candidates,
            reason="No repo-local rule document found. Do not create or write automatically.",
        )

    if len(candidates) == 1:
        return RuleTargetResolution(
            status="resolved",
            target=candidates[0].path,
            candidates=candidates,
            reason="Exactly one repo-local rule document found.",
        )

    canonical = detect_canonical_target(candidates)
    if canonical is not None:
        return RuleTargetResolution(
            status="resolved",
            target=canonical,
            candidates=candidates,
            reason="Multiple rule documents found, but one canonical target is explicitly indicated or contents are identical.",
        )

    return RuleTargetResolution(
        status="proposal-only",
        target=None,
        candidates=candidates,
        reason="Multiple active rule documents found and no canonical target is explicit. Compare manually and propose only.",
    )


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Resolve repo-local rule document target for rule promotion.")
    parser.add_argument("repo_root")
    args = parser.parse_args()

    result = resolve_rule_target(args.repo_root)
    print(json.dumps({
        "status": result.status,
        "target": str(result.target) if result.target else None,
        "reason": result.reason,
        "candidates": [
            {"name": c.name, "path": str(c.path), "size": c.size, "preview": c.preview}
            for c in result.candidates
        ],
    }, ensure_ascii=False, indent=2))
