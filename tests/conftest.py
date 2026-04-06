"""
claude-review-pdca テストの共通フィクスチャ。
"""
import sqlite3
import pytest
import sys
from pathlib import Path
from unittest.mock import patch

# hooks/ を import パスに追加（テスト対象モジュールの解決用）
HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def in_memory_db():
    """findings テーブルを持つインメモリ SQLite DB を返す。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE findings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          TEXT,
            repo_root           TEXT,
            reviewer            TEXT NOT NULL,
            finding_summary     TEXT NOT NULL,
            severity            TEXT NOT NULL CHECK(severity IN ('critical','high','warning','info','nitpick')),
            category            TEXT,
            resolution          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(resolution IN ('pending','accepted','rejected_intentional','rejected_wrong','fixed','stale')),
            abstracted_pattern  TEXT,
            project             TEXT,
            file_path           TEXT,
            score               INTEGER,
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
            resolved_at         TEXT,
            dismissed           INTEGER NOT NULL DEFAULT 0,
            dismissed_by        TEXT,
            dismissed_at        TEXT,
            fp_reason           TEXT,
            injected_count      INTEGER NOT NULL DEFAULT 0,
            last_injected       TEXT,
            last_relevant_edit  TEXT
        );

        CREATE TABLE IF NOT EXISTS review_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT,
            reviewer      TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
            started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
            closed_at     TEXT,
            findings_count INTEGER DEFAULT 0,
            close_reason  TEXT
        );
    """)
    yield conn
    conn.close()


@pytest.fixture
def sample_findings(in_memory_db):
    """テスト用の findings を挿入した DB を返す。"""
    conn = in_memory_db
    conn.executemany("""
        INSERT INTO findings (session_id, repo_root, reviewer, finding_summary, severity, category, file_path, resolution, dismissed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        ("sess1", "C:/project", "ifr", "SQL injection risk", "critical", "security", "C:/project/hooks/db.py", "pending", 0),
        ("sess1", "C:/project", "ifr", "Missing error handling", "high", "robustness", "C:/project/hooks/main.py", "pending", 0),
        ("sess1", "C:/project", "ifr", "Unused import", "warning", "cleanup", "C:/project/hooks/main.py", "pending", 0),
        ("sess1", "C:/project", "ifr", "Info level note", "info", "style", "C:/project/hooks/main.py", "pending", 0),
        ("sess1", "C:/project", "ifr", "Already fixed", "high", "security", "C:/project/hooks/db.py", "fixed", 0),
        ("sess1", "C:/project", "ifr", "Dismissed FP", "warning", "security", "C:/project/hooks/db.py", "pending", 1),
    ])
    conn.commit()
    return conn
