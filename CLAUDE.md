# Claude Code Instructions - claude-review-pdca

## プロジェクト概要

Claude Code レビュースキル（/ifr 等）の findings を一元管理し、
次回実装時に自動サーフェスする PDCA サイクルシステム。

**既存インフラ:**
- `C:\Users\Tenormusica\.claude\review-feedback.db` — findings 蓄積 SQLite
- `C:\Users\Tenormusica\.claude\scripts\review-feedback.py` — DB 操作 CLI
- `C:\Users\Tenormusica\.claude\settings.json` — hook 登録先

**⚠️ DB 実態メモ（ドキュメントと異なる点）:**
- テーブル名: `findings`（`review_feedback` ではない）
- カラム名: `finding_summary`（`summary` ではない）
- resolution デフォルト: `'pending'`（NULL ではない）
- resolution CHECK: `pending` / `accepted` / `rejected_intentional` / `rejected_wrong` / `fixed` / `stale`
- `repo_root TEXT` カラム追加済み（リポジトリスコープ分離用）
- `dismissed` / `fp_reason` / `injected_count` / `last_injected` / `last_relevant_edit` カラム追加済み
- インデックス: `idx_file_path` / `idx_pending` / `idx_repo_file(repo_root, file_path)` 作成済み

## 絶対ルール

### dismissed は人間のみが承認できる
- `dismissed_by = 'user'` 以外の dismissed 処理は禁止
- Claude が自己判断で dismissed にすることは禁止
- 「false positive かも」と思ったら **提案して承認を得る**
- 詳細: `docs/dismissal-policy.md`

### PostToolUse で毎回 /ifr を実行しない
- Edit のたびに自動レビューするとコスト爆発
- バッチ方式: 5 編集ごと or セッション末に提案する
- 詳細: `docs/hooks.md`

## ドキュメント構成

| ファイル | 内容 |
|---------|------|
| `docs/design.md` | システム設計・PDCA フロー全体像 |
| `docs/db-schema.md` | DB スキーマ・追加カラム・標準クエリ集 |
| `docs/hooks.md` | 全 hook の実装コード・settings.json 設定 |
| `docs/dismissal-policy.md` | dismissed 安全ルール（必読）|
| `docs/references.md` | Karpathy・Boris Cherny 等の参考知見 |

## 実装フェーズ

### Phase 1（完了）
- [x] `hooks/pre-tool-inject-findings.py` 実装
- [x] `review-feedback.db` に `dismissed` / `fp_reason` 等のカラム追加
- [x] `settings.json` に PreToolUse hook 登録
- [x] SessionStart hook を high/critical 件数分割表示に更新

### Phase 2（完了）
- [x] `hooks/post-tool-edit-counter.py` 実装（バッチカウント + セッション内編集ファイル追跡）
- [x] `hooks/session-end-learn.py` 実装（プロジェクト固有 CLAUDE.md に追記・repo_root スコープフィルタ付き）
- [x] `scripts/batch-review-trigger.py` 実装（5編集ごとのバッチレビュー起動・編集ファイルリスト付きレポート）

### Phase 3（完了）
- [x] `review-feedback.py dismiss` コマンド追加（ユーザー承認フロー）
- [x] 全体動作テスト・チューニング

### Phase 4（完了 — Grok 4 外部レビュー指摘対応）
- [x] `repo_root TEXT` カラム追加 + `idx_repo_file` インデックス（クロスプロジェクト汚染防止）
- [x] Phase A 注入クエリに repo_root スコープフィルタ追加（`OR repo_root IS NULL` 旧データフォールバック付き）
- [x] resolution ライフサイクル拡張: `fixed`（コミット解決）/ `stale`（TTL 期限切れ）
- [x] `gc-stale` CLI コマンド追加（90日超 pending → stale 自動遷移）
- [x] NOT EXISTS サブクエリに `fixed` resolution を追加（解決済み findings の再注入防止）
- [x] `session-end-learn.py` 学習クエリに repo_root フィルタ + severity ガード（critical 除外）
- [x] `_get_project_root` 二重呼び出し最適化（Phase B で Phase A の repo_root を再利用）

### Phase 5（完了 — Grok 4 ベストプラクティス調査に基づく改善）
- [x] dismiss ディスカバラビリティ（注入テキストに finding ID + dismiss コマンド例を追加）
- [x] FP 理由の PreToolUse 注入（学習済みパターンを注入ブロック末尾に追加）
- [x] `last_relevant_edit TEXT` カラム追加 + PostToolUse での更新 + PreToolUse の OR 鮮度条件

## 学習済み false positive パターン（自動生成）
- [security] テスト用の誤検知 （2回承認）
