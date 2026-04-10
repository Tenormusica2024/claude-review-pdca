"""
SessionStart hook と review-feedback summary の E2E テスト。

実 home / 実 DB を汚さず、temp home + temp repo + temp DB + temp JSONL で
subprocess 実行して reviewer 粒度の GLM alert 可視化まで通す。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GLOBAL_CLAUDE_DIR = Path.home() / ".claude"
RUN_KWARGS = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}


def _copy_required_runtime_files(temp_home: Path) -> tuple[Path, Path]:
    repo_dir = temp_home / "claude-review-pdca"
    (repo_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (repo_dir / "hooks").mkdir(parents=True, exist_ok=True)
    (temp_home / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (temp_home / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)

    shutil.copy2(
        REPO_ROOT / "scripts" / "summarize-glm-fallbacks.py",
        repo_dir / "scripts" / "summarize-glm-fallbacks.py",
    )
    shutil.copy2(
        REPO_ROOT / "scripts" / "record-rfl-patterns.py",
        repo_dir / "scripts" / "record-rfl-patterns.py",
    )
    shutil.copy2(
        REPO_ROOT / "hooks" / "config.py",
        repo_dir / "hooks" / "config.py",
    )
    shutil.copy2(
        REPO_ROOT / "hooks" / "glm_classifier.py",
        repo_dir / "hooks" / "glm_classifier.py",
    )
    shutil.copy2(
        GLOBAL_CLAUDE_DIR / "scripts" / "review-feedback.py",
        temp_home / ".claude" / "scripts" / "review-feedback.py",
    )
    shutil.copy2(
        GLOBAL_CLAUDE_DIR / "hooks" / "review-feedback-session-check.js",
        temp_home / ".claude" / "hooks" / "review-feedback-session-check.js",
    )
    shutil.copy2(
        GLOBAL_CLAUDE_DIR / "hooks" / "implementation-session-detector.js",
        temp_home / ".claude" / "hooks" / "implementation-session-detector.js",
    )
    shutil.copy2(
        REPO_ROOT / "hooks" / "pre-tool-inject-findings.py",
        repo_dir / "hooks" / "pre-tool-inject-findings.py",
    )
    shutil.copy2(
        REPO_ROOT / "hooks" / "pattern_db.py",
        repo_dir / "hooks" / "pattern_db.py",
    )

    return repo_dir, temp_home / ".claude" / "review-feedback.db"


def _make_env(temp_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["USERPROFILE"] = str(temp_home)
    env["HOME"] = str(temp_home)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@dataclass
class E2ERuntime:
    temp_home: Path
    repo_dir: Path
    db_path: Path
    env: dict[str, str]

    @property
    def repo_root(self) -> str:
        return str(self.repo_dir).replace("\\", "/")

    def run(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        params = dict(RUN_KWARGS)
        params.update(kwargs)
        return subprocess.run(command, cwd=self.repo_dir, env=self.env, **params)

    def python(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.run([sys.executable, *args], **kwargs)

    def node(self, script_path: Path, **kwargs) -> subprocess.CompletedProcess:
        return self.run(["node", str(script_path)], **kwargs)

    def write_target_file(self, relative_path: str, content: str = "# test target\n") -> None:
        target = self.repo_dir / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _create_runtime(tmp_path: Path) -> E2ERuntime:
    temp_home = tmp_path / "home"
    repo_dir, db_path = _copy_required_runtime_files(temp_home)
    env = _make_env(temp_home)
    subprocess.run(["git", "init"], cwd=repo_dir, env=env, check=True, capture_output=True, text=True)
    return E2ERuntime(temp_home=temp_home, repo_dir=repo_dir, db_path=db_path, env=env)


@pytest.fixture
def e2e_runtime(tmp_path) -> E2ERuntime:
    return _create_runtime(tmp_path)


def _seed_review_feedback_db(db_path: Path, repo_root: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO findings (
                repo_root, reviewer, finding_summary, severity, category, resolution,
                project, file_path, dismissed, injected_count
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 0, 0)
            """,
            (
                repo_root,
                "review-fix-loop",
                "rate-limit fallback should stay visible",
                "warning",
                "robustness",
                "claude-review-pdca",
                "hooks/glm_classifier.py",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_glm_fallback_log(temp_home: Path, repo_root: str) -> None:
    log_path = temp_home / ".claude" / "logs" / "glm-classifier-fallbacks.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts": "2026-04-10T00:00:00+00:00",
            "reason": "http_429",
            "source": "fallback",
            "severity": "warning",
            "reviewer": "review-fix-loop",
            "repo_root": repo_root,
            "summary_preview": "first 429",
        },
        {
            "ts": "2026-04-10T00:00:01+00:00",
            "reason": "suppressed_http_429",
            "source": "fallback",
            "severity": "warning",
            "reviewer": "review-fix-loop",
            "repo_root": repo_root,
            "summary_preview": "suppressed",
        },
        {
            "ts": "2026-04-10T00:00:02+00:00",
            "reason": "http_429",
            "source": "fallback",
            "severity": "warning",
            "reviewer": "intent-first-review",
            "repo_root": repo_root,
            "summary_preview": "other reviewer",
        },
        {
            "ts": "2026-04-10T00:00:03+00:00",
            "reason": "http_429",
            "source": "fallback",
            "severity": "warning",
            "reviewer": "review-fix-loop",
            "repo_root": repo_root,
            "summary_preview": "second 429",
        },
    ]
    with log_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _seed_pretool_findings_db(db_path: Path, repo_root: str, file_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                repo_root TEXT,
                reviewer TEXT NOT NULL,
                finding_summary TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT,
                resolution TEXT NOT NULL DEFAULT 'pending',
                abstracted_pattern TEXT,
                project TEXT,
                file_path TEXT,
                score INTEGER,
                created_at TEXT NOT NULL DEFAULT '2026-04-10T00:00:00',
                resolved_at TEXT,
                dismissed INTEGER NOT NULL DEFAULT 0,
                dismissed_at TEXT,
                dismissed_by TEXT,
                fp_reason TEXT,
                injected_count INTEGER NOT NULL DEFAULT 0,
                last_injected TEXT,
                last_relevant_edit TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO findings (
                repo_root, reviewer, finding_summary, severity, category, resolution,
                project, file_path, dismissed, injected_count, last_relevant_edit
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 0, 0, '2026-04-10T00:00:00')
            """,
            (
                repo_root,
                "review-fix-loop",
                "remember the pending robustness finding",
                "warning",
                "robustness",
                "claude-review-pdca",
                file_path,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_patterns_db(temp_home: Path, file_path: str, repo_root: str) -> None:
    db_path = temp_home / ".claude" / "review-patterns.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                pattern_text TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'warning',
                file_path TEXT,
                repo_root TEXT,
                confidence TEXT NOT NULL DEFAULT 'high',
                detection_count INTEGER NOT NULL DEFAULT 1,
                first_detected TEXT NOT NULL,
                last_detected TEXT NOT NULL,
                source_finding_id INTEGER,
                source_reviewer TEXT,
                pattern_embedding TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO patterns (
                category, pattern_text, severity, file_path, repo_root,
                confidence, detection_count, first_detected, last_detected, source_reviewer
            ) VALUES (?, ?, ?, ?, ?, 'high', 2, '2026-04-10T00:00:00', '2026-04-10T00:00:00', ?)
            """,
            (
                "logic",
                "avoid reintroducing stale learned pattern",
                "warning",
                file_path,
                repo_root,
                "review-fix-loop",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for hook E2E")
@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for hook E2E")
def test_session_start_hook_and_summary_e2e(e2e_runtime):
    runtime = e2e_runtime

    runtime.python(
        str(runtime.temp_home / ".claude" / "scripts" / "review-feedback.py"), "query", "--limit", "1",
        check=True,
    )

    _seed_review_feedback_db(runtime.db_path, runtime.repo_root)
    _seed_glm_fallback_log(runtime.temp_home, runtime.repo_root)

    summary = runtime.python(
        str(runtime.temp_home / ".claude" / "scripts" / "review-feedback.py"), "summary",
        check=True,
    )
    assert "GLM classifier fallback summary:" in summary.stdout
    assert "recent by reviewer:" in summary.stdout
    assert "review-fix-loop: http_429=2, suppressed_http_429=1" in summary.stdout

    hook_result = runtime.node(
        runtime.temp_home / ".claude" / "hooks" / "review-feedback-session-check.js",
        input=json.dumps({"cwd": runtime.repo_root}),
        check=True,
    )
    payload = json.loads(hook_result.stdout)
    additional_context = payload["hookSpecificOutput"]["additionalContext"]

    assert "=== PENDING REVIEW FINDINGS: 1件 ===" in additional_context
    assert "=== GLM CLASSIFIER SOFT ALERT ===" in additional_context
    assert "- recent http_429: 3 件" in additional_context
    assert "- recent suppressed_http_429: 1 件" in additional_context
    assert "- top reviewer: review-fix-loop (http_429=2, suppressed_http_429=1)" in additional_context


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for hook E2E")
@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for hook E2E")
def test_implementation_gate_and_pretool_learned_patterns_e2e(e2e_runtime):
    runtime = e2e_runtime
    target_file = "src/app/main.py"
    runtime.write_target_file(target_file)

    _seed_pretool_findings_db(runtime.db_path, runtime.repo_root, target_file)
    _seed_patterns_db(runtime.temp_home, target_file, runtime.repo_root)

    plain_result = runtime.python(
        str(runtime.repo_dir / "hooks" / "pre-tool-inject-findings.py"),
        input=json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": target_file},
            "session_id": "sess-plain",
            "cwd": runtime.repo_root,
        }),
        check=True,
    )
    assert "=== PAST FINDINGS:" in plain_result.stdout
    assert "[LEARNED PATTERNS]" not in plain_result.stdout

    detector_result = runtime.node(
        runtime.temp_home / ".claude" / "hooks" / "implementation-session-detector.js",
        input=json.dumps({
            "session_id": "sess-impl",
            "cwd": runtime.repo_root,
            "user_prompt": "/rfl この learned pattern の挙動を確認",
        }),
        check=True,
    )
    assert detector_result.stdout == ""

    gate_file = runtime.temp_home / ".claude" / "hooks" / "implementation-session.json"
    gate = json.loads(gate_file.read_text(encoding="utf-8"))
    assert gate["session_id"] == "sess-impl"
    assert gate["repo_root"] == runtime.repo_root
    assert "/rfl" in gate["matched_markers"]

    gated_result = runtime.python(
        str(runtime.repo_dir / "hooks" / "pre-tool-inject-findings.py"),
        input=json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": target_file},
            "session_id": "sess-impl",
            "cwd": runtime.repo_root,
        }),
        check=True,
    )
    assert "=== PAST FINDINGS:" in gated_result.stdout
    assert "[LEARNED PATTERNS]" in gated_result.stdout
    assert "avoid reintroducing stale learned pattern" in gated_result.stdout

    dedup_result = runtime.python(
        str(runtime.repo_dir / "hooks" / "pre-tool-inject-findings.py"),
        input=json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": target_file},
            "session_id": "sess-impl",
            "cwd": runtime.repo_root,
        }),
    )
    assert dedup_result.returncode == 0
    assert dedup_result.stdout == ""


def test_review_feedback_record_bridges_to_pattern_db_e2e(e2e_runtime):
    runtime = e2e_runtime

    findings = json.dumps([
        {
            "summary": "auth token leak in debug output",
            "severity": "high",
            "file_path": f"{runtime.repo_root}/src/app/main.py",
        }
    ], ensure_ascii=False)

    for _ in range(2):
        record_result = runtime.python(
            str(runtime.temp_home / ".claude" / "scripts" / "review-feedback.py"),
            "record",
            "--reviewer",
            "review-fix-loop",
            "--repo-root",
            runtime.repo_root,
            "--findings",
            findings,
            check=True,
        )
        payload = json.loads(record_result.stdout)
        assert payload["inserted_ids"]

    findings_conn = sqlite3.connect(runtime.db_path)
    findings_conn.row_factory = sqlite3.Row
    try:
        finding_rows = findings_conn.execute(
            "SELECT reviewer, finding_summary, severity, file_path, repo_root FROM findings ORDER BY id"
        ).fetchall()
    finally:
        findings_conn.close()

    assert len(finding_rows) == 2
    assert finding_rows[0]["reviewer"] == "review-fix-loop"
    assert finding_rows[0]["severity"] == "high"
    assert finding_rows[0]["file_path"] == "src/app/main.py"
    assert finding_rows[0]["repo_root"] == runtime.repo_root

    patterns_db_path = runtime.temp_home / ".claude" / "review-patterns.db"
    assert patterns_db_path.exists()

    patterns_conn = sqlite3.connect(patterns_db_path)
    patterns_conn.row_factory = sqlite3.Row
    try:
        pattern_rows = patterns_conn.execute(
            """
            SELECT category, pattern_text, severity, file_path, repo_root, detection_count, source_reviewer
            FROM patterns
            ORDER BY id
            """
        ).fetchall()
    finally:
        patterns_conn.close()

    assert len(pattern_rows) == 1
    assert pattern_rows[0]["category"] == "security"
    assert pattern_rows[0]["pattern_text"] == "auth token leak in debug output"
    assert pattern_rows[0]["severity"] == "warning"
    assert pattern_rows[0]["file_path"] == "src/app/main.py"
    assert pattern_rows[0]["repo_root"] == runtime.repo_root
    assert pattern_rows[0]["detection_count"] == 2
    assert pattern_rows[0]["source_reviewer"] == "review-fix-loop"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for hook E2E")
@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for hook E2E")
def test_recorded_pattern_reinjected_via_implementation_gate_e2e(e2e_runtime):
    runtime = e2e_runtime
    target_file = "src/app/main.py"
    runtime.write_target_file(target_file)

    findings = json.dumps([
        {
            "summary": "auth token leak in debug output",
            "severity": "high",
            "file_path": f"{runtime.repo_root}/{target_file}",
        }
    ], ensure_ascii=False)

    for _ in range(2):
        runtime.python(
            str(runtime.temp_home / ".claude" / "scripts" / "review-feedback.py"),
            "record",
            "--reviewer",
            "review-fix-loop",
            "--repo-root",
            runtime.repo_root,
            "--findings",
            findings,
            check=True,
        )

    runtime.node(
        runtime.temp_home / ".claude" / "hooks" / "implementation-session-detector.js",
        input=json.dumps({
            "session_id": "sess-loop",
            "cwd": runtime.repo_root,
            "user_prompt": "/review-fix-loop この file の再発防止を見ながら修正",
        }),
        check=True,
    )

    inject_result = runtime.python(
        str(runtime.repo_dir / "hooks" / "pre-tool-inject-findings.py"),
        input=json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": target_file},
            "session_id": "sess-loop",
            "cwd": runtime.repo_root,
        }),
        check=True,
    )

    assert "=== PAST FINDINGS:" in inject_result.stdout
    assert "auth token leak in debug output" in inject_result.stdout
    assert "[LEARNED PATTERNS]" in inject_result.stdout
    assert "[security] auth token leak in debug output (2回検出)" in inject_result.stdout
