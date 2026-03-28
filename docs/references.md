# 参考知見

## Andrej Karpathy（Karpathy Loop / AutoResearch, 2026）

### vibe coding（2025年2月）
- 「完全に vibes に身を委ね、指数関数的な成長を受け入れ、コードが存在することを忘れる」
- LLM が優秀なため「見る→言う→実行→コピペ」で進める
- X投稿: https://x.com/karpathy/status/1886192184808149383

### Karpathy Loop（self-improvement loop）
- 630行 Python の完全閉ループで self-improvement を実現
- 構成: ① agent が編集可能な training code、② 固定の experiment runner、③ markdown log
- 流れ: 仮説立案 → コード編集 → 実験実行 → データ収集 → 最適化 → commit → 次の iteration
- 700実験/2日を自動化（human in the loop を完全排除）

### このシステムへの示唆
- 「review → edit → re-run test → commit」の PDCA を agent 自身が回す
- ログ（=findings DB）が次の iteration の入力になる閉ループ構造

---

## Boris Cherny（Claude Code 創作者）

### 発言（2025-2026）
- CLAUDE.md を memory system として活用した **ruthless self-improvement**
- 修正が発生するたびに「同じミスを二度と繰り返さない」記述を CLAUDE.md に追加
- Meta 時代からのレビューパターン蓄積 → lint 化 → AI に拡張
- 「AI が自分のコードの 80% を書く」状態で、修正 → CLAUDE.md 更新 → 次セッション即反映が標準
- 参考: https://newsletter.pragmaticengineer.com/p/building-claude-code-with-boris-cherny

### このシステムへの示唆
- CLAUDE.md は additive（追記型）が正しい。dismissal は subtractive（削除型）で危険
- correction → 即時 CLAUDE.md 更新 → 次セッションで自動ロード
- → SessionEnd hook での自動追記パターンに直接対応

---

## 共通アーキテクチャパターン（2026 harness engineering）

### Minimal harness + persistent memory

```
最小構成:
- SQLite（findings 蓄積）
- CLAUDE.md（学習済みルール）
- PreToolUse hook（知見の能動的注入）
```

外部サービス（Pinecone / ChromaDB）は不要。
既存の SQLite + hook 構成に追加カラムと hook スクリプトだけで実現できる。

### Loopy Era（2026 トレンド）
- agent が自分で experiment loop を回す時代
- 「human が承認・品質保証を行い、実装は agent に委ねる」がプロ版 vibe coding
- このシステムはその基盤: agent が過去の失敗を参照しながら自律的に改善する

---

## 実装パターンの収束

WebSearch と Grok4 Expert の両調査で収束した推奨構成:

| 項目 | 推奨 | 理由 |
|------|------|------|
| 保存先 | SQLite（git管理可） | 外部依存ゼロ、チーム共有可能 |
| 取り出し方 | file_path フィルタ（WHERE） | シンプル・高速・確実 |
| 取り出しタイミング | PreToolUse hook | 編集確定時のみ・コンテキスト汚染なし |
| レビュートリガー | バッチ（5編集ごと） | コスト・UX のバランス |
| 学習 | CLAUDE.md 追記（additive） | Boris Cherny パターン |
| dismissed | ユーザー承認必須 | Sycophancy Trap 防止 |
