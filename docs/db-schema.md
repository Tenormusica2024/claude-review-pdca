# DB スキーマ

## 実際のスキーマ（review-feedback.db）

> **重要**: テーブル名は `findings`（`review_feedback` ではない）。
> `PRAGMA table_info(findings)` で確認済み。

```sql
-- 実テーブル（2026-03-29 確認）
CREATE TABLE findings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT,
    reviewer            TEXT NOT NULL,
    finding_summary     TEXT NOT NULL,        -- カラム名は "summary" ではなく "finding_summary"
    severity            TEXT NOT NULL,        -- "critical" / "high" / "warning" / "info"
    category            TEXT,
    resolution          TEXT NOT NULL DEFAULT 'pending',  -- "pending" / "fixed" / "accepted" 等
    abstracted_pattern  TEXT,
    project             TEXT,
    file_path           TEXT,
    score               INTEGER,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    resolved_at         TEXT
);
```

## 追加カラム（ALTER TABLE で拡張）

これらのカラムは**まだ存在しない**。Phase 1 実装時に追加する。

```sql
-- dismissed 管理（ユーザー承認専用）
ALTER TABLE findings ADD COLUMN dismissed      BOOLEAN   DEFAULT 0;
ALTER TABLE findings ADD COLUMN dismissed_by   TEXT;     -- "user" のみ（"claude" 禁止）
ALTER TABLE findings ADD COLUMN dismissed_at   TIMESTAMP;
ALTER TABLE findings ADD COLUMN fp_reason      TEXT;     -- false positive の理由

-- 注入トラッキング（SNR 改善用）
ALTER TABLE findings ADD COLUMN injected_count INTEGER DEFAULT 0;
ALTER TABLE findings ADD COLUMN last_injected  TIMESTAMP;
```

## 拡張後の完全スキーマ

```sql
CREATE TABLE findings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT,
    reviewer            TEXT NOT NULL,
    finding_summary     TEXT NOT NULL,
    severity            TEXT NOT NULL CHECK(severity IN ('critical','high','warning','info')),
    category            TEXT,
    resolution          TEXT NOT NULL DEFAULT 'pending',
    abstracted_pattern  TEXT,
    project             TEXT,
    file_path           TEXT,
    score               INTEGER,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    resolved_at         TEXT,
    -- 追加カラム（Phase 1 で ALTER TABLE）
    dismissed           BOOLEAN   DEFAULT 0,
    dismissed_by        TEXT,           -- "user" のみ許可
    dismissed_at        TIMESTAMP,
    fp_reason           TEXT,
    injected_count      INTEGER   DEFAULT 0,
    last_injected       TIMESTAMP
);
```

## インデックス（注入クエリ高速化）

```sql
-- PreToolUse hook の WHERE file_path = ? クエリを高速化
CREATE INDEX IF NOT EXISTS idx_file_path
    ON findings (file_path, dismissed, severity);

-- SessionStart の pending 件数カウント高速化
CREATE INDEX IF NOT EXISTS idx_pending
    ON findings (dismissed, resolution, severity);
```

## 標準クエリ集

### ファイル特化注入クエリ（PreToolUse hook 用）

```sql
SELECT id, severity, category, finding_summary
FROM findings
WHERE file_path = :target_file
  AND dismissed = 0
  AND resolution = 'pending'
  AND severity IN ('critical', 'high', 'warning')
  AND created_at >= datetime('now', '-30 days')
ORDER BY
  CASE severity
    WHEN 'critical' THEN 0
    WHEN 'high'     THEN 1
    WHEN 'warning'  THEN 2
    ELSE 3
  END,
  id DESC
LIMIT 8;
```

### pending 件数（SessionStart 通知用）

```sql
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical,
  SUM(CASE WHEN severity = 'high'     THEN 1 ELSE 0 END) AS high
FROM findings
WHERE dismissed = 0
  AND resolution = 'pending';
```

### dismissed パターン集計（SessionEnd → CLAUDE.md 追記用）

```sql
SELECT category, fp_reason, COUNT(*) AS count
FROM findings
WHERE dismissed = 1
  AND dismissed_by = 'user'
GROUP BY category, fp_reason
ORDER BY count DESC;
```

### dismiss 操作（ユーザー承認時のみ実行）

```sql
UPDATE findings
SET dismissed    = 1,
    dismissed_by = 'user',
    dismissed_at = CURRENT_TIMESTAMP,
    fp_reason    = :reason
WHERE id = :finding_id;
```
