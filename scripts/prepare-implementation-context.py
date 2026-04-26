#!/usr/bin/env python3
"""
Codex / 手動運用向けの implementation context 準備コマンド。

役割:
1. 実装系スキル/コマンド文字列から implementation gate を起動する
2. PreToolUse hook 相当の context 注入文を明示コマンドで取得する

例:
  python scripts/prepare-implementation-context.py ^
    --session-id codex-sess-1 ^
    --cwd C:/repo ^
    --prompt "sc-rfl この file を修正" ^
    --file-path src/app/main.py
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

from config import IMPLEMENTATION_SESSION_PATH, PROJECT_ROOT, normalize_git_root

IMPLEMENTATION_MARKERS = [
    "/review-fix-loop",
    "/rfl",
    "/iterative-fix",
    "/ui-fix",
    "sc-rfl",
    "sc-review-fix-loop",
    "sc-ui",
    "sc-frontend-implementation",
    "sc-tdd",
    "sc-e2e",
    "sc-bt",
    "sc-at",
]


def detect_implementation_markers(text: str) -> list[str]:
    if not text:
        return []
    return [marker for marker in IMPLEMENTATION_MARKERS if marker in text]


def detect_repo_root(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return normalize_git_root(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    current = Path(cwd).resolve()
    while True:
        if (current / ".git").exists():
            return str(current).replace("\\", "/")
        if current.parent == current:
            break
        current = current.parent
    return str(Path(cwd).resolve()).replace("\\", "/")


def write_implementation_gate(session_id: str, cwd: str, matched_markers: list[str]) -> dict:
    metadata = {
        "detected_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "session_id": session_id,
        "cwd": str(Path(cwd).resolve()).replace("\\", "/"),
        "repo_root": detect_repo_root(cwd),
        "matched_markers": matched_markers,
    }
    IMPLEMENTATION_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMPLEMENTATION_SESSION_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata


def build_hook_payload(session_id: str, cwd: str, tool_name: str, file_paths: list[str]) -> dict:
    normalized = [p.replace("\\", "/") for p in file_paths]
    if tool_name == "MultiEdit" or len(normalized) > 1:
        return {
            "tool_name": "MultiEdit",
            "tool_input": {
                "edits": [{"file_path": p} for p in normalized],
            },
            "session_id": session_id,
            "cwd": str(Path(cwd).resolve()).replace("\\", "/"),
        }
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": normalized[0]},
        "session_id": session_id,
        "cwd": str(Path(cwd).resolve()).replace("\\", "/"),
    }


def run_pretool_injection(payload: dict) -> subprocess.CompletedProcess[str]:
    hook_path = PROJECT_ROOT / "hooks" / "pre-tool-inject-findings.py"
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="implementation gate 起動 + PDCA context 注入")
    parser.add_argument("--session-id", required=True, help="セッションID（dedup と gate に使用）")
    parser.add_argument("--cwd", default=".", help="対象 repo の cwd")
    parser.add_argument(
        "--file-path",
        action="append",
        required=True,
        help="編集対象ファイル。複数指定可",
    )
    parser.add_argument(
        "--tool-name",
        choices=("Edit", "Write", "MultiEdit"),
        default="Edit",
        help="想定ツール名。複数 file-path 指定時は自動で MultiEdit 化",
    )
    parser.add_argument("--prompt", default="", help="実装系スキル/コマンドを含む user prompt")
    parser.add_argument(
        "--marker",
        action="append",
        default=[],
        help="implementation gate を強制起動したい marker（sc-rfl 等）",
    )
    args = parser.parse_args()

    matched_markers = []
    matched_markers.extend(detect_implementation_markers(args.prompt))
    matched_markers.extend([m for m in args.marker if m])
    matched_markers = list(dict.fromkeys(matched_markers))

    if matched_markers:
        metadata = write_implementation_gate(args.session_id, args.cwd, matched_markers)
        print(
            f"[prepare-implementation-context] implementation gate activated: "
            f"{', '.join(metadata['matched_markers'])}",
            file=sys.stderr,
        )

    payload = build_hook_payload(
        session_id=args.session_id,
        cwd=args.cwd,
        tool_name=args.tool_name,
        file_paths=args.file_path,
    )
    result = run_pretool_injection(payload)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.stdout:
        print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
