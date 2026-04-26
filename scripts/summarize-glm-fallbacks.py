#!/usr/bin/env python3
"""
GLM classifier fallback ログを集計する簡易スクリプト。

使い方:
  python scripts/summarize-glm-fallbacks.py
  python scripts/summarize-glm-fallbacks.py --limit 10
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from config import GLM_FALLBACK_LOG_PATH


def _load_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []

    events = []
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events


def _filter_events(
    events: list[dict],
    repo_root: str | None = None,
    reviewer: str | None = None,
) -> list[dict]:
    filtered = events
    if repo_root:
        normalized_repo = repo_root.replace("\\", "/").rstrip("/")
        filtered = [
            event for event in filtered
            if (event.get("repo_root") or "").replace("\\", "/").rstrip("/") == normalized_repo
        ]
    if reviewer:
        filtered = [event for event in filtered if (event.get("reviewer") or "") == reviewer]
    return filtered


def _build_recent_reviewer_summary(events: list[dict], limit: int) -> dict[str, dict[str, int]]:
    recent = events[-limit:] if limit > 0 else []
    recent_by_reviewer: dict[str, Counter] = {}
    for event in recent:
        reviewer = event.get("reviewer") or "unknown"
        reviewer_counts = recent_by_reviewer.setdefault(reviewer, Counter())
        reviewer_counts[event.get("reason", "unknown")] += 1
    return {
        reviewer: dict(counts)
        for reviewer, counts in recent_by_reviewer.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize GLM classifier fallback events")
    parser.add_argument("--limit", type=int, default=5, help="Show N most recent events")
    parser.add_argument("--log-path", type=str, default=str(GLM_FALLBACK_LOG_PATH), help="Override JSONL log path")
    parser.add_argument("--repo-root", type=str, help="Filter by repo_root")
    parser.add_argument("--reviewer", type=str, help="Filter by reviewer")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")
    args = parser.parse_args()

    events = _filter_events(
        _load_events(Path(args.log_path)),
        repo_root=args.repo_root,
        reviewer=args.reviewer,
    )
    if not events:
        if args.json:
            print(json.dumps({
                "total": 0,
                "by_reason": {},
                "by_severity": {},
                "by_reviewer": {},
                "by_repo_root": {},
                "recent_by_reviewer": {},
                "recent": [],
            }, ensure_ascii=False))
        else:
            print("fallback events: 0")
        return

    by_reason = Counter(event.get("reason", "unknown") for event in events)
    by_severity = Counter(event.get("severity", "unknown") for event in events)
    by_reviewer = Counter(event.get("reviewer") or "unknown" for event in events)
    by_repo_root = Counter(event.get("repo_root") or "unknown" for event in events)
    recent = events[-args.limit:]
    recent_by_reviewer = _build_recent_reviewer_summary(events, args.limit)

    if args.json:
        print(json.dumps({
            "total": len(events),
            "by_reason": dict(by_reason),
            "by_severity": dict(by_severity),
            "by_reviewer": dict(by_reviewer),
            "by_repo_root": dict(by_repo_root),
            "recent_by_reviewer": recent_by_reviewer,
            "recent": recent,
        }, ensure_ascii=False))
        return

    print(f"fallback events: {len(events)}")
    print("by reason:")
    for reason, count in by_reason.most_common():
        print(f"  {reason}: {count}")

    print("by severity:")
    for severity, count in by_severity.most_common():
        print(f"  {severity}: {count}")

    print("by reviewer:")
    for reviewer, count in by_reviewer.most_common():
        print(f"  {reviewer}: {count}")

    if recent_by_reviewer:
        print(f"recent by reviewer ({min(args.limit, len(events))}):")
        for reviewer, counts in sorted(
            recent_by_reviewer.items(),
            key=lambda item: sum(item[1].values()),
            reverse=True,
        ):
            parts = ", ".join(
                f"{reason}={count}"
                for reason, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
            )
            print(f"  {reviewer}: {parts}")

    print("by repo_root:")
    for repo_root, count in by_repo_root.most_common():
        print(f"  {repo_root}: {count}")

    print(f"recent {min(args.limit, len(events))}:")
    for event in recent:
        print(
            f"  {event.get('ts')} | {event.get('reason')} | "
            f"{event.get('severity')} | {event.get('summary_preview')}"
        )


if __name__ == "__main__":
    main()
