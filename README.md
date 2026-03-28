# claude-review-pdca

Claude Code レビュースキル（/ifr 等）で見つかった findings を一元管理し、
次回実装時に自動サーフェスする PDCA サイクルシステム。

## 問題意識

- `/ifr` `/review-fix-loop` などのレビュースキルは findings（バグ・設計問題・アンチパターン）を検出する
- 現状は `review-feedback.db`（SQLite）に保存されるが、次の実装セッションに自動的に流れ込まない
- SessionStart hook で「pending N 件」と通知されるだけで、全件注入するとコンテキストが汚染される

## 解決アプローチ

**編集対象ファイルが確定した瞬間に、そのファイルに関連する過去 findings だけをピンポイント注入する。**

```
Edit/Write 実行
    ↓ PreToolUse hook 起動
    ↓ SQLite: file_path フィルタ + dismissed 除外 + severity 降順 LIMIT 8
    ↓ 関連 findings のみコンテキスト注入（SNR 維持）
    ↓ Claude が実装（過去の失敗を参照しながら）
    ↓ セッション末 / 5編集ごと: /ifr トリガー（バッチ方式）
    ↓ 新 findings を DB 保存
    ↓ SessionEnd: confirmed dismissed パターンを CLAUDE.md に追記
```

## ドキュメント構成

| ファイル | 内容 |
|---------|------|
| `docs/design.md` | システム設計・アーキテクチャ詳細 |
| `docs/db-schema.md` | DB スキーマ・拡張計画 |
| `docs/hooks.md` | hook 実装仕様（PreToolUse / PostToolUse / SessionEnd）|
| `docs/dismissal-policy.md` | dismissed 学習の設計方針・安全ルール |
| `docs/references.md` | Karpathy・Boris Cherny 等の参考知見 |
| `CLAUDE.md` | プロジェクト固有の Claude Code 指示 |

## 関連ファイル（既存インフラ）

- `C:\Users\Tenormusica\.claude\review-feedback.db` — findings DB
- `C:\Users\Tenormusica\.claude\scripts\review-feedback.py` — DB 操作 CLI
- `C:\Users\Tenormusica\.claude\settings.json` — hook 設定
