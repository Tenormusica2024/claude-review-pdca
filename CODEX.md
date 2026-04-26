# claude-review-pdca: Codex runtime notes

## Purpose
- Claude hook が使えない実行環境でも、review findings / learned patterns の再注入を再現する。
- Codex では hook の代わりに `scripts/prepare-implementation-context.py` を使う。

## When to activate
次のどれかが prompt / task に含まれる実装セッションでは、**最初の Edit / Write / MultiEdit 前** に context 注入を走らせる。

- `sc-rfl`
- `sc-review-fix-loop`
- `sc-ui`
- `sc-frontend-implementation`
- `sc-tdd`
- `sc-e2e`
- `sc-bt`
- `sc-at`
- `/review-fix-loop`
- `/rfl`
- `/iterative-fix`
- `/ui-fix`

## Codex activation rule
- 対象ファイルがまだ分からない段階では、先に編集対象を特定する。
- 対象ファイルが分かったら、編集前に次を実行する。
- 同一 session で追加の対象ファイルが増えた場合は、その file-path を足して再実行してよい。

```powershell
python scripts/prepare-implementation-context.py `
  --session-id "<session-id>" `
  --cwd "<repo-root>" `
  --tool-name Edit `
  --prompt "<original-user-prompt>" `
  --file-path "path/to/target.py"
```

## Notes
- `--prompt` に marker が含まれていれば implementation session として自動判定される。
- 明示したい場合は `--marker sc-rfl` のように追加してよい。
- `REVIEW_FEEDBACK_SCRIPT` 環境変数で `review-feedback.py` の場所を上書きできる。
- Claude hook runtime では `hooks/implementation-session-detector.js` / `hooks/review-feedback-session-check.js` が同等の役割を担う。
