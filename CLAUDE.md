# Claude Code Instructions - claude-review-pdca

## プロジェクト概要

Claude Code レビュースキル（/ifr 等）の findings を一元管理し、
次回実装時に自動サーフェスする PDCA サイクルシステム。

**既存インフラ:**
- `C:\Users\Tenormusica\.claude\review-feedback.db` — findings 蓄積 SQLite
- `C:\Users\Tenormusica\.claude\scripts\review-feedback.py` — DB 操作 CLI
- `C:\Users\Tenormusica\.claude\settings.json` — hook 登録先

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

### Phase 1（優先実装）
- [ ] `hooks/pre-tool-inject-findings.py` 実装
- [ ] `review-feedback.db` に `dismissed` / `fp_reason` 等のカラム追加
- [ ] `settings.json` に PreToolUse hook 登録
- [ ] SessionStart hook を high/critical フィルタ版に更新

### Phase 2
- [ ] `hooks/post-tool-edit-counter.py` 実装（バッチカウント）
- [ ] `hooks/session-end-learn.py` 実装（CLAUDE.md 追記）

### Phase 3
- [ ] `review-feedback.py dismiss` コマンド追加（ユーザー承認フロー）
- [ ] 全体動作テスト・チューニング
