#!/usr/bin/env python3
"""
RFL（review-fix-loop）完了後にパターンを review-patterns.db に記録するブリッジスクリプト。

使い方:
  # JSON 形式で findings を渡す
  python scripts/record-rfl-patterns.py --findings '[{"summary":"...", "severity":"critical", "category":"logic", "file_path":"..."}]'

  # ファイルから読み込み
  python scripts/record-rfl-patterns.py --findings-file /tmp/rfl-findings.json

  # repo_root を明示指定（省略時は CWD の git root を自動検出）
  python scripts/record-rfl-patterns.py --findings '[...]' --repo-root "C:/Users/Tenormusica/project"
"""
import sys
import json
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pattern_db import record_pattern, validate_category
from config import normalize_git_root


def _detect_repo_root() -> str | None:
    """CWD の git リポジトリルートを取得する。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return normalize_git_root(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="RFL findings → review-patterns.db 記録")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--findings", type=str, help="JSON 文字列で findings を渡す")
    group.add_argument("--findings-file", type=str, help="JSON ファイルパス")
    parser.add_argument("--repo-root", type=str, default=None, help="リポジトリルート（省略時は自動検出）")
    parser.add_argument("--reviewer", type=str, default="review-fix-loop", help="レビュアー名")
    args = parser.parse_args()

    # findings 読み込み
    if args.findings:
        try:
            findings = json.loads(args.findings)
        except json.JSONDecodeError as e:
            print(f"JSON パースエラー: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        findings_path = Path(args.findings_file)
        if not findings_path.exists():
            print(f"ファイルが見つかりません: {args.findings_file}", file=sys.stderr)
            sys.exit(1)
        try:
            findings = json.loads(findings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"JSON パースエラー ({args.findings_file}): {e}", file=sys.stderr)
            sys.exit(1)

    if not isinstance(findings, list):
        print("findings は配列である必要があります", file=sys.stderr)
        sys.exit(1)

    # repo_root の決定
    repo_root = args.repo_root or _detect_repo_root()

    # severity フィルタ: warning 以上のみ記録（info/nitpick は学習対象外）
    # review-feedback.db の severity IN ('critical', 'high', 'warning') と一致させる
    valid_severities = {"critical", "high", "warning"}
    recorded = 0
    skipped = 0

    for f in findings:
        severity = f.get("severity", "warning").lower()
        if severity not in valid_severities:
            skipped += 1
            continue

        summary = f.get("summary", "")
        if not summary:
            skipped += 1
            continue

        category = f.get("category", "maintainability")
        file_path = f.get("file_path")
        # file_path の正規化（バックスラッシュ→スラッシュ）
        if file_path:
            file_path = file_path.replace("\\", "/")

        # pattern_db は severity を critical/warning のみ受け付ける（high → warning にマッピング）
        pattern_id = record_pattern(
            category=category,
            pattern_text=summary,
            severity="critical" if severity == "critical" else "warning",
            file_path=file_path,
            repo_root=repo_root,
            confidence="high",
            source_reviewer=args.reviewer,
        )
        recorded += 1

    print(f"記録: {recorded} 件, スキップ: {skipped} 件")


if __name__ == "__main__":
    main()
