#!/usr/bin/env python3
"""
learned patterns 注入ログを集計する簡易スクリプト。
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from config import LEARNED_PATTERN_LOG_PATH


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


def _normalize_path(value: str | None) -> str:
    return str(value or "").replace("\\", "/").rstrip("/")


def _filter_events(
    events: list[dict],
    repo_root: str | None = None,
    file_path: str | None = None,
    reviewer: str | None = None,
) -> list[dict]:
    filtered = events
    if repo_root:
        normalized_repo_root = _normalize_path(repo_root)
        filtered = [
            event for event in filtered
            if _normalize_path(event.get("repo_root")) == normalized_repo_root
        ]
    if file_path:
        normalized_file_path = _normalize_path(file_path)
        filtered = [
            event for event in filtered
            if _normalize_path(event.get("file_path")) == normalized_file_path
        ]
    if reviewer:
        filtered = [
            event for event in filtered
            if str(event.get("reviewer") or "") == reviewer
        ]
    return filtered


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize learned pattern injection events")
    parser.add_argument("--limit", type=int, default=5, help="Show N most recent events")
    parser.add_argument("--log-path", type=str, default=str(LEARNED_PATTERN_LOG_PATH), help="Override JSONL log path")
    parser.add_argument("--repo-root", type=str, help="Filter by repo_root")
    parser.add_argument("--file-path", type=str, help="Filter by file_path")
    parser.add_argument("--reviewer", type=str, help="Filter by reviewer")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")
    args = parser.parse_args()

    events = _filter_events(
        _load_events(Path(args.log_path)),
        repo_root=args.repo_root,
        file_path=args.file_path,
        reviewer=args.reviewer,
    )

    if not events:
        payload = {
            "total": 0,
            "by_repo_root": {},
            "by_file_path": {},
            "by_tool_name": {},
            "by_reviewer": {},
            "by_category": {},
            "recent": [],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("learned pattern injections: 0")
        return

    by_repo_root = Counter(_normalize_path(event.get("repo_root")) or "unknown" for event in events)
    by_file_path = Counter(_normalize_path(event.get("file_path")) or "unknown" for event in events)
    by_tool_name = Counter(str(event.get("tool_name") or "unknown") for event in events)
    by_reviewer = Counter(str(event.get("reviewer") or "unknown") for event in events)
    by_category = Counter()
    for event in events:
        for category in event.get("categories") or []:
            by_category[str(category)] += 1

    recent = events[-args.limit:] if args.limit > 0 else []
    payload = {
        "total": len(events),
        "by_repo_root": dict(by_repo_root),
        "by_file_path": dict(by_file_path),
        "by_tool_name": dict(by_tool_name),
        "by_reviewer": dict(by_reviewer),
        "by_category": dict(by_category),
        "recent": recent,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return

    print(f"learned pattern injections: {len(events)}")
    print("by repo_root:")
    for repo_root, count in by_repo_root.most_common():
        print(f"  {repo_root}: {count}")
    print("by file_path:")
    for file_path, count in by_file_path.most_common():
        print(f"  {file_path}: {count}")
    print("by tool_name:")
    for tool_name, count in by_tool_name.most_common():
        print(f"  {tool_name}: {count}")
    print("by reviewer:")
    for reviewer, count in by_reviewer.most_common():
        print(f"  {reviewer}: {count}")
    print("by category:")
    for category, count in by_category.most_common():
        print(f"  {category}: {count}")
    print(f"recent {len(recent)}:")
    for event in recent:
        print(
            f"  {event.get('ts')} | {event.get('tool_name')} | "
            f"{event.get('file_path')} | patterns={event.get('pattern_count')}"
        )


if __name__ == "__main__":
    main()
