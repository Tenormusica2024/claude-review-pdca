# システム設計

## PDCA サイクル全体像

```
[Plan]  PreToolUse hook → 過去 findings をファイル特化で注入
[Do]    Claude が実装（過去の失敗を参照済み）
[Check] バッチレビュー（5編集ごと or セッション末に /ifr）
[Act]   新 findings を DB 保存 → confirmed dismissed を CLAUDE.md 反映
```

## コンポーネント構成

```
claude-review-pdca/
├── hooks/
│   ├── pre-tool-inject-findings.py   # PreToolUse: ファイル特化注入
│   └── session-end-learn.py          # SessionEnd: dismissed → CLAUDE.md 反映
├── scripts/
│   └── batch-review-trigger.py       # [Phase 2] 5編集ごとのバッチレビュー起動（未実装）
└── docs/
    ├── design.md        （本ファイル）
    ├── db-schema.md
    ├── hooks.md
    ├── dismissal-policy.md
    └── references.md
```

## 設計原則

### 1. ピンポイント注入（コンテキスト汚染防止）

全 findings をコンテキストに流し込まない。
編集対象ファイルに紐づく findings のみ、LIMIT 8 件に絞って注入する。

```sql
-- ⚠️ dismissed カラムは Phase 1 ALTER TABLE 完了後に有効（追加前は AND dismissed = 0 を除くこと）
SELECT id, severity, category, finding_summary
FROM findings
WHERE file_path = :target_file
  AND dismissed = 0
  AND resolution = 'pending'
ORDER BY
  CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
  id DESC
LIMIT 8;
```

### 2. dismissed は人間が承認する（エージェント自己判断禁止）

Claude が自分のコードのレビュー findings を却下するのは「シンプル化トラップ」。
自分に不都合な findings を都合よく dismissed にする可能性がある。

→ **dismissed は必ずユーザーが明示的に承認する。Claude の自己判断での dismissed 処理は禁止。**

唯一の例外: 全く同一の finding（同ファイル・同カテゴリ・同 summary）が既に `resolution = 'fixed'` の場合のみ自動スキップ。dismissed finding は対象外。

### 3. バッチレビュー（PostToolUse 毎回トリガー禁止）

Edit のたびに /ifr を自動起動すると：
- セッションで 10〜20 回のレビューパスが走る
- トークンコストが爆発
- ユーザー体験が悪化（毎回止まる）

→ **「5編集ごと」または「セッション末」のバッチ方式を採用。**

トリガー条件:
- 同一セッションで Edit/Write が累計 5 回に達した時
- ユーザーが明示的に `/review` または `/ifr` を呼び出した時
- セッション終了時（SessionEnd hook）

### 4. CLAUDE.md 連携（Boris Cherny パターン）

学習した dismissed パターン（ユーザー承認済み）を SessionEnd で CLAUDE.md に追記。
次セッション開始時に自動ロードされ、同じ false positive が再発しなくなる。

追記フォーマット:
```markdown
## 学習済み false positive パターン（自動追記）
- [ファイルパス or カテゴリ]: [パターン説明] — [承認日]
```

### 5. SNR（Signal-to-Noise Ratio）維持フィルタ

注入時に除外するもの:
- dismissed = 1（ユーザー承認済みの不要 finding）
- resolution != 'pending'（解決済み・accepted 等）
- severity = 'info'（低優先度。critical/high/warning のみ注入）
- 直近 30 日以上前の finding（陳腐化リスク）

```sql
-- ⚠️ dismissed カラムは Phase 1 ALTER TABLE 完了後に有効（追加前は AND dismissed = 0 を除くこと）
AND dismissed = 0
AND resolution = 'pending'
AND severity IN ('critical', 'high', 'warning')
AND created_at >= datetime('now', '-30 days')
```

## 注入文テンプレート

PreToolUse hook が生成するコンテキスト注入文の形式:

```
=== PAST FINDINGS: {file_path} ===
【critical】{category}: {finding_summary}
【high】{category}: {finding_summary}
【warning】{category}: {finding_summary}
（{N}件中上位{M}件を表示）
これらを考慮して編集してください。同じアンチパターンの繰り返しは避けること。
=== END FINDINGS ===
```

## データフロー図

```
レビュースキル (/ifr, /review-fix-loop)
    │
    ▼ findings 生成
review-feedback.py record
    │
    ▼ INSERT INTO findings
review-feedback.db (SQLite)
    │
    ├──▶ PreToolUse hook (pre-tool-inject-findings.py)
    │        │ file_path フィルタ + SNR フィルタ
    │        ▼
    │    コンテキスト注入 → Claude 実装
    │
    ├──▶ バッチレビュー (batch-review-trigger.py)
    │        │ 5編集ごと or セッション末
    │        ▼
    │    /ifr 実行 → 新 findings → DB 追記
    │
    └──▶ SessionEnd hook (session-end-learn.py)
             │ dismissed（ユーザー承認済み）集計
             ▼
         CLAUDE.md に false positive パターン追記
```
