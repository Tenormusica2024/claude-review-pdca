# DB スキーマ

## 現在のスキーマ（review-feedback.db）

```sql
-- 既存テーブル（変更なし）
CREATE TABLE IF NOT EXISTS review_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer    TEXT NOT NULL,        -- "review-fix-loop", "ifr" 等
    file_path   TEXT,                 -- 対象ファイルパス
    summary     TEXT NOT NULL,        -- finding の概要
    severity    TEXT NOT NULL,        -- "critical" / "high" / "warning" / "info"
    category    TEXT,                 -- "bug" / "design" / "anti-pattern" 等
    resolution  TEXT,                 -- "fixed" / "dismissed" / NULL（未解決）
    session_id  TEXT                  -- セッション識別子
);
```

## 追加カラム（ALTER TABLE で拡張）

```sql
-- dismissed 管理（ユーザー承認専用）
ALTER TABLE review_feedback ADD COLUMN dismissed      BOOLEAN   DEFAULT 0;
ALTER TABLE review_feedback ADD COLUMN dismissed_by   TEXT;     -- "user" のみ（"claude" 禁止）
ALTER TABLE review_feedback ADD COLUMN dismissed_at   TIMESTAMP;
ALTER TABLE review_feedback ADD COLUMN fp_reason      TEXT;     -- false positive の理由

-- 注入トラッキング（SNR 改善用）
ALTER TABLE review_feedback ADD COLUMN injected_count INTEGER DEFAULT 0;  -- 何回注入されたか
ALTER TABLE review_feedback ADD COLUMN last_injected  TIMESTAMP;          -- 最後に注入された日時
ALTER TABLE review_feedback ADD COLUMN created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
```

## 拡張後の完全スキーマ

```sql
CREATE TABLE IF NOT EXISTS review_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer        TEXT NOT NULL,
    file_path       TEXT,
    summary         TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK(severity IN ('critical','high','warning','info')),
    category        TEXT,
    resolution      TEXT,
    session_id      TEXT,
    -- 追加カラム
    dismissed       BOOLEAN   DEFAULT 0,
    dismissed_by    TEXT,           -- "user" のみ許可
    dismissed_at    TIMESTAMP,
    fp_reason       TEXT,
    injected_count  INTEGER   DEFAULT 0,
    last_injected   TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## インデックス（注入クエリ高速化）

```sql
-- PreToolUse hook の WHERE file_path = ? クエリを高速化
CREATE INDEX IF NOT EXISTS idx_file_path
    ON review_feedback (file_path, dismissed, severity);

-- SessionStart の pending 件数カウント高速化
CREATE INDEX IF NOT EXISTS idx_pending
    ON review_feedback (dismissed, resolution, severity);
```

## 標準クエリ集

### ファイル特化注入クエリ（PreToolUse hook 用）

```sql
SELECT severity, category, summary
FROM review_feedback
WHERE file_path = :target_file
  AND dismissed = 0
  AND resolution IS NULL
  AND severity IN ('critical', 'high', 'warning')
  AND (created_at IS NULL OR created_at >= datetime('now', '-30 days'))
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
FROM review_feedback
WHERE dismissed = 0
  AND resolution IS NULL;
```

### dismissed パターン集計（SessionEnd → CLAUDE.md 追記用）

```sql
SELECT category, fp_reason, COUNT(*) AS count
FROM review_feedback
WHERE dismissed = 1
  AND dismissed_by = 'user'
GROUP BY category, fp_reason
ORDER BY count DESC;
```

### dismiss 操作（ユーザー承認時のみ実行）

```sql
UPDATE review_feedback
SET dismissed    = 1,
    dismissed_by = 'user',
    dismissed_at = CURRENT_TIMESTAMP,
    fp_reason    = :reason
WHERE id = :finding_id;
```
