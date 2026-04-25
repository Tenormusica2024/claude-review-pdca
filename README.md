# claude-review-pdca

[![test](https://github.com/Tenormusica2024/claude-review-pdca/actions/workflows/test.yml/badge.svg?branch=master)](https://github.com/Tenormusica2024/claude-review-pdca/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

A closed-loop PDCA (Plan-Do-Check-Act) system that automatically surfaces past code review findings during implementation, enabling Claude Code to learn from its own mistakes.

## The Problem

Claude Code's review skills (`/ifr`, `/review-fix-loop`, etc.) detect bugs, design issues, and anti-patterns -- but findings are stored once and forgotten. The next coding session starts from zero, with no memory of past mistakes. A bulk "you have N pending findings" notification at session start is noise; developers ignore it.

## The Solution

**Inject the right findings at the right time**: when a file is being edited, only past findings for *that specific file* are surfaced -- keeping signal-to-noise ratio high.

```
Developer edits a file
     |
     v
PreToolUse hook fires
     |
     v
SQLite query: file_path filter + severity + freshness + repo scope
     |
     v
Relevant findings injected into context (max 8)
     |
     v
Claude implements with awareness of past mistakes
     |
     v
Every 5 edits: batch review trigger
     |
     v
SessionEnd: learned false-positive patterns written to CLAUDE.md
```

## Architecture

```
                    +------------------+
                    |  review-feedback |
                    |      .db         |  <-- SQLite: findings, sessions, dismissals
                    +--------+---------+
                             |
              +--------------+--------------+
              |              |              |
     +--------v---+  +------v------+  +----v---------+
     | PreToolUse |  | PostToolUse |  |  SessionEnd  |
     |   Hook     |  |    Hook     |  |    Hook      |
     +--------+---+  +------+------+  +----+---------+
              |              |              |
   File-specific      Edit counter    FP pattern
   finding injection  + file tracking  learning
   (Phase A/B)        (batch trigger)  + stale GC
              |              |              |
              v              v              v
     Claude context   batch-review-    CLAUDE.md
     (SNR-optimized)  trigger.py       auto-update
```

### Hook Pipeline

| Hook | Trigger | Purpose |
|------|---------|---------|
| **PreToolUse** | Before Edit/Write | Inject past findings for the target file (Phase A: file-specific, Phase B: project-wide fallback) |
| **PostToolUse** | After Edit/Write | Count edits, track modified files, update finding freshness |
| **SessionEnd** | Session close | Learn confirmed false-positive patterns, clean up state files, GC stale findings |

## Key Design Decisions

### 1. Pinpoint Injection over Bulk Notification

Instead of dumping all pending findings at session start (context pollution), findings are injected **only when the relevant file is being edited**. This keeps the signal-to-noise ratio high -- Claude sees 3-8 relevant findings, not 50 unrelated ones.

### 2. Two-Phase Query Strategy

- **Phase A**: File-specific findings filtered by severity, freshness (30-day window OR recent `last_relevant_edit`), and repo scope
- **Phase B**: If Phase A returns nothing, fall back to project-wide critical-only findings (max 5)

This ensures high-severity issues are never missed, even for files without prior findings.

### 3. Human-Only Dismissal (Anti-Sycophancy Guard)

Claude cannot dismiss its own findings. The `dismissed_by` column enforces `'user'` -- preventing the "sycophancy trap" where an AI marks its own flagged issues as false positives to appear helpful. Only after a human confirms a pattern 2+ times does it get promoted to `CLAUDE.md`.

### 4. Batch Review Trigger

Running `/ifr` after every single edit would explode costs. Instead, edits are counted silently, and a structured review is triggered every 5 edits with a prioritized report of which files to review.

### 5. Resolution Lifecycle

Findings follow a state machine: `pending` -> `accepted` | `rejected_intentional` | `rejected_wrong` | `fixed` | `stale`. The `stale` transition (90 days) is automatic via SessionEnd GC, preventing unbounded growth.

### 6. Repository Scoping

`repo_root` column prevents cross-project contamination -- findings from project A never leak into project B's injection context. Path normalization handles Windows backslashes, UNC paths (`//server/share`), and case differences.

## Project Structure

```
claude-review-pdca/
  hooks/
    config.py                       # Shared config (DB path, normalize_git_root)
    pre-tool-inject-findings.py     # PreToolUse: file-specific finding injection
    post-tool-edit-counter.py       # PostToolUse: edit counting + file tracking
    session-end-learn.py            # SessionEnd: FP learning + cleanup + stale GC
  scripts/
    batch-review-trigger.py         # 5-edit batch review coordinator
    backfill-repo-root.py           # Migration: backfill repo_root for legacy data
  tests/
    conftest.py                     # Pytest fixtures (in-memory SQLite)
    test_config.py                  # Config module tests
    test_pre_tool_inject_findings.py
    test_pre_tool_inject_main.py    # Phase A/B injection logic
    test_post_tool_edit_counter.py
    test_batch_review_trigger.py
    test_session_end_learn.py
  docs/
    design.md                       # System architecture deep-dive
    db-schema.md                    # Full schema + standard queries
    hooks.md                        # Hook implementation specs
    dismissal-policy.md             # Safety rules for FP dismissal
    references.md                   # Karpathy, Boris Cherny, etc.
  CLAUDE.md                         # Project-specific Claude Code instructions
```

## Database Schema (Key Columns)

```sql
CREATE TABLE findings (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT,
    repo_root       TEXT,           -- repository scope isolation
    reviewer        TEXT,
    severity        TEXT,           -- critical / high / warning / info
    category        TEXT,
    file_path       TEXT,
    finding_summary TEXT,
    resolution      TEXT DEFAULT 'pending',
        -- CHECK: pending / accepted / rejected_intentional
        --        / rejected_wrong / fixed / stale
    dismissed       INTEGER DEFAULT 0,
    dismissed_by    TEXT,           -- 'user' only (anti-sycophancy)
    fp_reason       TEXT,
    last_relevant_edit TEXT,        -- freshness signal for dormant findings
    created_at      TEXT,
    ...
);
```

**Indexes**: `idx_file_path`, `idx_pending`, `idx_repo_file(repo_root, file_path)`

## Inspiration

- **Andrej Karpathy** -- "Vibe coding" and the feedback loop between AI coding assistants and code quality
- **Boris Cherny** -- Self-correcting repositories: every mistake becomes a rule that prevents the next one
- **PDCA Cycle** -- Continuous improvement applied to AI-assisted development: Plan (review rules) -> Do (implement) -> Check (findings) -> Act (learn patterns)

## Tech Stack

- **Python 3.12+** with type hints
- **SQLite** for findings persistence (zero-dependency, single-file DB)
- **Claude Code Hooks API** (PreToolUse / PostToolUse / SessionEnd)
- **pytest** for testing with in-memory SQLite fixtures

## License

MIT
