#!/usr/bin/env python3
"""
共通 review outcome payload を PDCA 保存先へ分流する producer 初版。

役割:
1. reviewer / path / item を正規化
2. payload 内 items を review-feedback.db 向け pending findings と
   review-patterns.db 向け pattern candidates に分流
3. 既存 CLI (`review-feedback.py`, `record-rfl-patterns.py`) を bridge として呼び出す
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import PROJECT_ROOT, REVIEW_FEEDBACK_SCRIPT, normalize_git_root

REVIEWER_ALIASES = {
    "sc-rfl": "review-fix-loop",
    "sc-review-fix-loop": "review-fix-loop",
    "/review-fix-loop": "review-fix-loop",
    "/rfl": "review-fix-loop",
    "review-fix-loop": "review-fix-loop",
    "sc-ifr": "intent-first-review",
    "/ifr": "intent-first-review",
    "ifr": "intent-first-review",
    "/intent-first-review": "intent-first-review",
    "intent-first-review": "intent-first-review",
    "sc-gr": "go-robust",
    "go-robust": "go-robust",
    "/go-robust": "go-robust",
    "sc-ir": "intent-review-light",
    "intent-review-light": "intent-review-light",
}

RECORDABLE_SEVERITIES = {"critical", "high", "warning"}
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def normalize_reviewer(value: str | None) -> str:
    normalized = str(value or "").strip()
    return REVIEWER_ALIASES.get(normalized, normalized or "unknown-reviewer")


def normalize_path(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).replace("\\", "/").rstrip("/")
    return normalized or None


def detect_repo_root(cwd: str = ".") -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return normalize_git_root(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def load_payload_from_args(args: argparse.Namespace) -> dict:
    if args.payload_json:
        try:
            payload = json.loads(args.payload_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"--payload-json のJSONが不正: {e}") from e
    else:
        payload_path = Path(args.payload_file)
        if not payload_path.exists():
            raise ValueError(f"payload file が見つかりません: {args.payload_file}")
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"payload file のJSONが不正: {e}") from e

    if not isinstance(payload, dict):
        raise ValueError("payload は object である必要があります")
    return payload


def normalize_item(item: dict, repo_root: str | None) -> dict:
    summary = str(item.get("summary") or "").strip()
    file_path = normalize_path(item.get("file_path"))
    line = item.get("line")
    if file_path and repo_root and file_path.startswith(repo_root.rstrip("/") + "/"):
        file_path = file_path[len(repo_root.rstrip("/") + "/"):]

    severity = str(item.get("severity") or "info").strip().lower()
    if severity not in {"critical", "high", "warning", "info", "nitpick"}:
        severity = "info"
    confidence = str(item.get("confidence") or "medium").strip().lower()
    if confidence not in CONFIDENCE_RANK:
        confidence = "medium"
    status = str(item.get("status") or "pending").strip().lower()
    category = str(item.get("category") or "").strip()

    return {
        "type": str(item.get("type") or "finding").strip(),
        "title": str(item.get("title") or "").strip(),
        "summary": summary,
        "severity": severity,
        "category": category,
        "file_path": file_path,
        "line": line,
        "status": status,
        "auto_fixable": bool(item.get("auto_fixable")),
        "needs_judgment": bool(item.get("needs_judgment")),
        "confidence": confidence,
    }


def should_record_feedback(item: dict, reviewer: str) -> bool:
    if item["type"] != "finding":
        return False
    if not item["summary"]:
        return False
    if item["severity"] not in RECORDABLE_SEVERITIES:
        return False
    if item["status"] not in {"pending", "judgment-required"}:
        return False
    if CONFIDENCE_RANK.get(item["confidence"], 0) < CONFIDENCE_RANK["medium"]:
        return False

    if reviewer == "intent-review-light":
        return bool(item["file_path"]) and CONFIDENCE_RANK.get(item["confidence"], 0) >= CONFIDENCE_RANK["high"]

    return True


def should_record_pattern(item: dict, reviewer: str) -> bool:
    if item["type"] != "finding":
        return False
    if not item["summary"]:
        return False
    if item["severity"] not in RECORDABLE_SEVERITIES:
        return False
    if CONFIDENCE_RANK.get(item["confidence"], 0) < CONFIDENCE_RANK["high"]:
        return False
    if not item["file_path"]:
        return False

    if reviewer == "intent-first-review":
        return item["status"] == "fixed"

    if reviewer == "go-robust":
        return item["status"] == "fixed"

    if reviewer == "intent-review-light":
        return item["status"] == "fixed"

    return item["status"] in {"fixed", "pending", "judgment-required"}


def build_feedback_findings(items: list[dict], reviewer: str) -> list[dict]:
    findings = []
    for item in items:
        if not should_record_feedback(item, reviewer):
            continue
        findings.append({
            "summary": item["summary"],
            "severity": item["severity"],
            "category": item["category"] or "maintainability",
            "file_path": item["file_path"],
        })
    return findings


def build_pattern_findings(items: list[dict], reviewer: str) -> list[dict]:
    findings = []
    for item in items:
        if not should_record_pattern(item, reviewer):
            continue
        findings.append({
            "summary": item["summary"],
            "severity": item["severity"],
            "category": item["category"] or "maintainability",
            "file_path": item["file_path"],
        })
    return findings


def _default_project_name(repo_root: str | None) -> str | None:
    if not repo_root:
        return None
    return Path(repo_root).name or None


def run_review_feedback_record(
    findings: list[dict],
    reviewer: str,
    session_id: str | None,
    repo_root: str | None,
    project: str | None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        REVIEW_FEEDBACK_SCRIPT,
        "record",
        "--reviewer", reviewer,
        "--findings", json.dumps(findings, ensure_ascii=False),
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])
    if project:
        cmd.extend(["--project", project])
    if repo_root:
        cmd.extend(["--repo-root", repo_root])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def run_pattern_record(
    findings: list[dict],
    reviewer: str,
    repo_root: str | None,
    classify: bool,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "record-rfl-patterns.py"),
        "--findings", json.dumps(findings, ensure_ascii=False),
        "--reviewer", reviewer,
    ]
    if repo_root:
        cmd.extend(["--repo-root", repo_root])
    if classify:
        cmd.append("--classify")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def build_summary(
    items: list[dict],
    reviewer: str,
    feedback_findings: list[dict],
    pattern_findings: list[dict],
    feedback_result: subprocess.CompletedProcess[str] | None,
    pattern_result: subprocess.CompletedProcess[str] | None,
) -> dict:
    feedback_ok = feedback_result is not None and feedback_result.returncode == 0
    pattern_ok = pattern_result is not None and pattern_result.returncode == 0
    routed_items = sum(
        1
        for item in items
        if should_record_feedback(item, reviewer) or should_record_pattern(item, reviewer)
    )
    judgment_items = sum(1 for item in items if item.get("needs_judgment"))
    ignored = max(len(items) - routed_items, 0)
    return {
        "recorded_feedback": len(feedback_findings) if feedback_ok else 0,
        "recorded_patterns": len(pattern_findings) if pattern_ok else 0,
        "judgment_items": judgment_items,
        "ignored_items": ignored,
        "feedback_error": None if feedback_ok or feedback_result is None else (feedback_result.stderr or feedback_result.stdout).strip() or "feedback record failed",
        "pattern_error": None if pattern_ok or pattern_result is None else (pattern_result.stderr or pattern_result.stdout).strip() or "pattern record failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Common review outcome → PDCA producer bridge")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--payload-json", help="review outcome payload JSON string")
    group.add_argument("--payload-file", help="review outcome payload JSON file")
    parser.add_argument("--cwd", default=".", help="repo root detection fallback cwd")
    parser.add_argument("--classify-patterns", action="store_true", help="pattern record 時に category 未設定 items を GLM 分類")
    args = parser.parse_args()

    try:
        payload = load_payload_from_args(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    reviewer = normalize_reviewer(payload.get("reviewer"))
    repo_root = normalize_path(payload.get("repo_root")) or detect_repo_root(args.cwd)
    session_id = str(payload.get("session_id") or "").strip() or None
    project = str(payload.get("project") or "").strip() or _default_project_name(repo_root)

    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list):
        print("Error: payload.items は配列である必要があります", file=sys.stderr)
        return 1

    normalized_items = [
        normalize_item(item, repo_root)
        for item in raw_items
        if isinstance(item, dict)
    ]

    feedback_findings = build_feedback_findings(normalized_items, reviewer)
    pattern_findings = build_pattern_findings(normalized_items, reviewer)

    feedback_result = None
    pattern_result = None

    if feedback_findings:
        feedback_result = run_review_feedback_record(
            feedback_findings,
            reviewer=reviewer,
            session_id=session_id,
            repo_root=repo_root,
            project=project,
        )
        if feedback_result.stderr:
            print(feedback_result.stderr, file=sys.stderr, end="")

    if pattern_findings:
        pattern_result = run_pattern_record(
            pattern_findings,
            reviewer=reviewer,
            repo_root=repo_root,
            classify=args.classify_patterns,
        )
        if pattern_result.stderr:
            print(pattern_result.stderr, file=sys.stderr, end="")

    summary = build_summary(
        items=normalized_items,
        reviewer=reviewer,
        feedback_findings=feedback_findings,
        pattern_findings=pattern_findings,
        feedback_result=feedback_result,
        pattern_result=pattern_result,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
