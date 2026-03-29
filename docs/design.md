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
│   ├── post-tool-edit-counter.py     # PostToolUse: 編集カウント管理（バッチトリガー用）
│   └── session-end-learn.py          # SessionEnd: dismissed → CLAUDE.md 反映
├── scripts/
│   └── batch-review-trigger.py       # 5編集ごとのバッチレビュー起動
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

**Phase A（ファイル特化）**: repo_root スコープフィルタで他プロジェクトの findings を除外。
`OR repo_root IS NULL` で旧データ（Phase 4 以前）にも 30 日間フォールバックする。
NOT EXISTS サブクエリで `resolution IN ('accepted', 'fixed')` 済みの同一パターンを除外。

**Phase B（プロジェクト横断フォールバック）**: Phase A が 0 件の場合、
`severity = 'critical'` のみ・LIMIT 5 でプロジェクトルート配下の findings を注入。
git root が 4 セグメント未満（ドライブルート等）の場合はスキップ。

詳細クエリは `docs/db-schema.md` の標準クエリ集を参照。

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
- resolution != 'pending'（解決済み・accepted・fixed・stale 等）
- severity = 'info' / 'nitpick'（低優先度。critical/high/warning のみ注入）
- 直近 30 日以上前の finding（陳腐化リスク）
- NOT EXISTS で同一パターンが既に `accepted` / `fixed` の finding は再注入しない

セッション内 dedup: `~/.claude/inject-state/{session_id}.txt` に注入済み ID を記録し、
同一セッションで同じ finding を重複注入しない。

### 6. リポジトリスコープ分離（クロスプロジェクト汚染防止）

`repo_root` カラムで finding をリポジトリ単位に分離する。
Phase A 注入クエリは `replace(COALESCE(repo_root, ''), '\\', '/') = ?` でスコープを絞り、
`OR repo_root IS NULL` で Phase 4 以前の旧データにもフォールバックする。

SessionEnd 学習クエリも同様に repo_root フィルタを適用し、
他プロジェクトの dismissed パターンが CLAUDE.md に混入するのを防止する。

### 7. resolution ライフサイクル

| 値 | 意味 | 遷移条件 |
|----|------|----------|
| `pending` | 未解決（デフォルト） | record 時に自動設定 |
| `accepted` | 指摘を受け入れて修正済み | ユーザー resolve |
| `rejected_intentional` | 意図的な設計として却下 | ユーザー resolve |
| `rejected_wrong` | 誤検知として却下 | ユーザー resolve |
| `fixed` | コミットで解決 | ユーザー resolve |
| `stale` | TTL 期限切れ | `gc-stale` コマンド（90日超 pending → stale） |

`gc-stale` CLI コマンド: 90 日超の pending findings を自動的に stale に遷移する。
定期的に実行することで DB の肥大化を防止する。

## 注入文テンプレート

PreToolUse hook が生成するコンテキスト注入文の形式:

```
=== PAST FINDINGS: {file_path} ===
【critical】{category}: {finding_summary}
【high】{category}: {finding_summary}
【warning】{category}: {finding_summary}
（{len(findings)} 件を表示）
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
    │        │ Phase A: file_path + repo_root フィルタ + SNR フィルタ
    │        │ Phase B: project-wide critical フォールバック
    │        ▼
    │    コンテキスト注入 → Claude 実装
    │
    ├──▶ バッチレビュー (batch-review-trigger.py)
    │        │ 5編集ごと or セッション末
    │        ▼
    │    /ifr 実行 → 新 findings → DB 追記
    │
    └──▶ SessionEnd hook (session-end-learn.py)
             │ dismissed（ユーザー承認済み）集計（repo_root スコープ付き）
             ▼
         CLAUDE.md に false positive パターン追記
```
