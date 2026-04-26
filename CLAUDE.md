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
| `docs/auto-pdca-producer-design.md` | sc-rfl / sc-ifr / sc-ir 共通 producer 設計 |
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

### Phase 6（完了 — IFR レビュー指摘対応・堅牢性改善）
- [x] `hooks/config.py` 共通設定モジュール抽出（DB_PATH 等の3ファイル重複排除）
- [x] NOT EXISTS サブクエリの repo_root スコープ修正（NULL 同士のみマッチ）
- [x] SessionEnd 学習クエリに `resolution = 'pending'` フィルタ追加
- [x] CLAUDE.md 自動生成ブロックの HTML マーカー分離（ユーザー手動追記の保護）
- [x] UNC パス先頭 `//` 保持（`//server/share` 破壊防止）
- [x] fp_reason サニタイズ（改行除去 + 80文字制限）
- [x] file_paths 収集時バックスラッシュ正規化
- [x] O_APPEND 原子性コメント Windows 対応
- [x] クリーンアップエラーの stderr ログ出力

### Phase 7（完了 — Grok 4 推奨 PDCA 改善）
- [x] `scripts/backfill-repo-root.py` — repo_root NULL バックフィル（Phase 1: git root + Phase 2: セッション推定）
- [x] dismiss コマンド簡略化（一括 dismiss + `--no-interactive` をコピペ即実行可能に）
- [x] stale GC を `session-end-learn.py` に組み込み（セッション終了時に90日超 pending を自動遷移）

### Phase 8（進行中 — PDCA v2: 全自動パターン学習サイクル）
- [x] `hooks/pattern_db.py` — 独立DB `review-patterns.db` のコアモジュール（13カテゴリenum、upsert、cool-off付き取得）
- [x] `pre-tool-inject-findings.py` — 学習済みパターン注入統合（findings + learned patterns の二層注入）
- [x] `scripts/record-rfl-patterns.py` — RFL完了後のパターン記録ブリッジスクリプト
- [x] `tests/test_pattern_db.py` — 21テスト（validate_category / record_pattern / get_patterns / format_injection）
- [x] `hooks/glm_classifier.py` — GLM-5.1 による13カテゴリ自動分類（OpenRouter API + フォールバック + リトライ）
- [x] `tests/test_glm_classifier.py` — 21テスト（fallback / extract_json / classify_finding / batch）
- [x] `record-rfl-patterns.py --classify` — GLM分類統合（`--classify` フラグで category 未設定 findings を自動分類）
- [x] learned-patterns 注入観測ログ + 集計スクリプト追加（2026-04-11）
- [ ] Phase 2: embedding重複排除、git diff推定、週次プルーニング

**設計原則:**
- **review-patterns.db** は review-feedback.db とは独立（実装バグのみ、スタイル・ドキュメントノイズ排除）
- **Cool-off**: detection_count >= 2 のパターンのみ注入（初回検出は学習しない → FP雪だるま防止）
- **13カテゴリenum**: logic, security, robustness, data-integrity, concurrency, type-safety, performance, api-contract, test-quality, consistency, documentation, ux, maintainability
- **3モデル戦略**: Opus（オーケストレーション）、GPT-5.4/Codex（レビュー・強い判断）、GLM-5.1（分類・軽量タスク）

## 📚 学んだ教訓
- session-end-learn が CLAUDE.md 不在プロジェクトをスキップする件: git リポジトリなのに CLAUDE.md がない場合は作成時の漏れとみなし、空の CLAUDE.md を新規作成する方針。ただしマスタールールが別ドキュメント（PROJECT_PROMPT.md 等）で管理されている場合は、移行可否をユーザーに確認してから CLAUDE.md にコピーし、旧ドキュメントはアーカイブに移動する
- git root 正規化ロジックは共通関数に集約する: 同一ロジックの3箇所重複はメンテナンスコストが高い。config.py に共通関数を置いて import する

## 本番運用 TODO（2026-04-10 調査）

### Critical
- [x] `~/.claude/settings.json` に `review-command-inject-hook.js` を登録済み（2026-04-10）
  - 影響: `/brutal-review` `/qc` `/pr-review` 開始時に `review-feedback.py inject` が構造的に保証されていない
- [x] `~/.claude/review-feedback.db` の live schema を現行コード前提に移行済み（2026-04-10）
  - 発見時: `severity` は `critical|warning|info|nitpick` のみ許可、`resolution` は `pending|accepted|rejected_intentional|rejected_wrong` のみ許可
  - 影響: `review-feedback.py` 現行コードが前提にする `high` / `fixed` / `stale` が live DB で使えない
- [x] `pre-tool-inject-findings.py` の absolute/relative path 互換照合を修正済み（2026-04-10）
  - 実測: pending findings の大半が relative path 保存で、編集前 inject が取りこぼされる

### High
- [x] `review-patterns.db` を live 環境で初期化済み（2026-04-10）
  - 影響: learned patterns 注入経路が静かにスキップされている
- [x] `scripts/record-rfl-patterns.py` を live `review-feedback.py record` から自動起動する接続を追加済み（2026-04-10）
  - 現状: `review-fix-loop` 記録時に `--classify` 付きブリッジを起動し、既存 accepted/fixed 履歴も backfill 済み
  - 実測: `~/.claude/review-patterns.db` の `patterns` 件数は 76
- [x] SessionStart の pending 通知を repo_root スコープ化済み（2026-04-10）
  - 現状: SessionStart hook が `cwd -> git root` を解決し、`review-feedback.py query --repo-root` で現在 repo の pending のみ通知
- [x] implementation skill 実行セッションだけ learned-patterns 注入を強める段階的ゲート設計を適用済み（2026-04-10）
  - 現状: UserPromptSubmit の implementation detector が `session_id + repo_root` を記録し、`PreToolUse(Edit|Write|MultiEdit)` はその gate が有効な session/repo でのみ learned-patterns を注入
  - 方針: critical/high findings は現状維持の安全網、learned patterns だけ implementation 文脈で段階的解放
  - residual: detector のマーカー集合は現行の実装系 slash command / skill 名ベース。将来の新コマンド追加時は追随が必要

### Notes
- `brutal-review` は `/intent-first-review` や `/review-fix-loop` に内部統合されているわけではない
  - `/review-fix-loop` は `/ifr` ベース、`/ifr` は intent-first-review ベースで動く
  - `brutal-review` の学習データが多いのは単体利用の履歴によるもの

## 学習済み false positive パターン（自動生成）
<!-- auto-generated:fp-patterns -->
- [security] テスト用の誤検知 （2回承認）
<!-- end-auto-generated:fp-patterns -->
