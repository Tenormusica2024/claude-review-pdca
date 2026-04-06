# DB スキーマ

## 実際のスキーマ（review-feedback.db）

> **重要**: テーブル名は `findings`（`review_feedback` ではない）。
> `PRAGMA table_info(findings)` で確認済み。

```sql
-- 実テーブル（現行スキーマ）
CREATE TABLE findings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT,
    repo_root           TEXT,                -- リポジトリルートパス（クロスプロジェクト分離用）
    reviewer            TEXT NOT NULL,
    finding_summary     TEXT NOT NULL,        -- カラム名は "summary" ではなく "finding_summary"
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
    -- dismissed 管理（ユーザー承認専用）
    dismissed           INTEGER NOT NULL DEFAULT 0,
    dismissed_by        TEXT,           -- "user" のみ許可（"claude" 禁止）
    dismissed_at        TEXT,
    fp_reason           TEXT,           -- false positive の理由
    -- 注入トラッキング（SNR 改善用）
    injected_count      INTEGER NOT NULL DEFAULT 0,
    last_injected       TEXT,
    -- 鮮度トラッキング（PostToolUse で更新）
    last_relevant_edit  TEXT           -- 関連ファイル編集時に更新。14日以内なら created_at が古くても注入対象
);
```

### resolution ライフサイクル

| 値 | 意味 | 遷移条件 |
|----|------|----------|
| `pending` | 未解決（デフォルト） | record 時に自動設定 |
| `accepted` | 指摘を受け入れて修正済み | ユーザー resolve |
| `rejected_intentional` | 意図的な設計として却下 | ユーザー resolve |
| `rejected_wrong` | 誤検知として却下 | ユーザー resolve |
| `fixed` | コミットで解決 | ユーザー resolve |
| `stale` | TTL 期限切れ | `gc-stale` コマンド（90日超 pending → stale） |

## review_sessions テーブル

レビューセッション（inject → record/close のライフサイクル）を管理するテーブル。

```sql
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
```

## インデックス

```sql
-- findings テーブル
CREATE INDEX IF NOT EXISTS idx_findings_reviewer ON findings(reviewer);
CREATE INDEX IF NOT EXISTS idx_findings_resolution ON findings(resolution);
CREATE INDEX IF NOT EXISTS idx_findings_reviewer_resolution ON findings(reviewer, resolution);
CREATE INDEX IF NOT EXISTS idx_findings_file_path ON findings(file_path);
CREATE INDEX IF NOT EXISTS idx_findings_pending ON findings(resolution, severity, created_at);
CREATE INDEX IF NOT EXISTS idx_repo_file ON findings(repo_root, file_path);

-- review_sessions テーブル
CREATE INDEX IF NOT EXISTS idx_review_sessions_status ON review_sessions(status);
```

## 標準クエリ集

### ファイル特化注入クエリ（PreToolUse hook Phase A）

repo_root スコープフィルタ付き。`OR repo_root IS NULL` で旧データ（Phase 4 以前の INSERT）にもマッチする。
repo_root が取得できない場合（git 管理外）は repo_root スコープフィルタなしで実行する（NOT EXISTS も同様にスコープなし）。

```sql
-- repo_root あり版（git 管理下）
SELECT id, severity, category, finding_summary
FROM findings
WHERE replace(file_path, '\', '/') = :normalized_path
  AND (replace(COALESCE(repo_root, ''), '\', '/') = :repo_root OR repo_root IS NULL)
  AND dismissed = 0
  AND resolution = 'pending'
  AND severity IN ('critical', 'high', 'warning')
  AND (created_at >= :cutoff OR COALESCE(last_relevant_edit, '2000-01-01') >= :relevance_cutoff)
  AND NOT EXISTS (
      SELECT 1 FROM findings f2
      WHERE replace(f2.file_path, '\', '/') = replace(findings.file_path, '\', '/')
        AND f2.category        = findings.category
        AND f2.finding_summary = findings.finding_summary
        AND f2.resolution      IN ('accepted', 'fixed')
        AND (replace(COALESCE(f2.repo_root, ''), '\', '/') = :repo_root
             OR (f2.repo_root IS NULL AND findings.repo_root IS NULL))
  )
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

### プロジェクト横断 critical フォールバック（PreToolUse hook Phase B）

Phase A が 0 件の場合のフォールバック。`severity = 'critical'` のみ、LIMIT 5。
`file_path LIKE :project_filter` でプロジェクトルート配下に限定（他プロジェクト混入防止）。
Phase A と同様に `last_relevant_edit` 鮮度 OR 条件を適用。

```sql
SELECT id, severity, category, finding_summary
FROM findings
WHERE dismissed = 0
  AND resolution = 'pending'
  AND severity = 'critical'
  AND (created_at >= :cutoff OR COALESCE(last_relevant_edit, '2000-01-01') >= :relevance_cutoff)
  AND LOWER(replace(file_path, '\', '/')) LIKE LOWER(:project_filter)
ORDER BY id DESC
LIMIT 5;
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

repo_root スコープ付き。`severity != 'critical'` で critical を学習対象から除外。
`resolution = 'pending'` で stale 化した findings を学習対象から除外。

```sql
SELECT category, fp_reason, COUNT(*) AS cnt
FROM findings
WHERE dismissed = 1
  AND dismissed_by = 'user'
  AND fp_reason IS NOT NULL AND fp_reason != ''
  AND severity != 'critical'
  AND resolution = 'pending'
  AND replace(COALESCE(repo_root, ''), '\', '/') = :repo_root
GROUP BY category, fp_reason
HAVING cnt >= 2
ORDER BY cnt DESC
LIMIT 20;
```

### 学習済み FP パターン取得（PreToolUse 注入用）

repo_root スコープ付き。ユーザーが 2 回以上 dismiss 承認したカテゴリ＋理由を集計。

```sql
SELECT category, fp_reason, COUNT(*) AS cnt
FROM findings
WHERE dismissed = 1
  AND dismissed_by = 'user'
  AND fp_reason IS NOT NULL AND fp_reason != ''
  AND severity != 'critical'
  AND resolution = 'pending'
  AND (replace(COALESCE(repo_root, ''), '\', '/') = :repo_root OR repo_root IS NULL)
GROUP BY category, fp_reason
HAVING cnt >= 2
ORDER BY cnt DESC
LIMIT 5;
```

### last_relevant_edit 更新（PostToolUse hook）

```sql
UPDATE findings
SET last_relevant_edit = :now
WHERE replace(file_path, '\', '/') = :normalized_path
  AND resolution = 'pending';
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

### gc-stale（90日超 pending → stale 自動遷移）

```sql
UPDATE findings
SET resolution = 'stale',
    resolved_at = CURRENT_TIMESTAMP
WHERE resolution = 'pending'
  AND created_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-90 days');
```
