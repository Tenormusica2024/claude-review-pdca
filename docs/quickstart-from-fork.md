# Quickstart from Fork

この文書は、`claude-review-pdca` を **fork / clone した人が最短で再注入の流れを確認するための骨組み**。

完全な運用自動化ではなく、まず:

1. `review-feedback.db` に finding がある
2. `prepare-implementation-context.py` で対象 file 向け context を出せる

の 2 点を最短で確かめることを目的にする。

---

## 推奨ディレクトリ構成

```text
<workspace>/
  claude-review-pdca/
  review-fix-pipeline/
```

この sibling repo 構成が最も分かりやすい。

---

## 前提

- Python 3.10+
- Git
- `claude-review-pdca` を clone 済み
- 可能なら `review-fix-pipeline` も sibling repo として clone 済み
- `review-feedback.py` の場所が分かる

必要なら session env で指定:

```powershell
$env:REVIEW_FEEDBACK_SCRIPT = "C:\path\to\review-fix-pipeline\scripts\review-feedback.py"
```

---

## Golden path

### 1. repo に移動

```powershell
cd C:\path\to\claude-review-pdca
```

### 2. implementation context を要求

```powershell
python scripts/prepare-implementation-context.py `
  --session-id demo-session `
  --cwd C:/path/to/actual-target-repo `
  --prompt "sc-rfl この file を修正" `
  --file-path src/app/main.py
```

期待:
- implementation marker が検出される
- `implementation-session.json` が更新される
- 対象 file に関係する finding があれば context block が出る

### 3. finding が無い場合

先に `review-fix-pipeline` 側で bridge を使って 1 件入れる:

```powershell
cd C:\path\to\review-fix-pipeline
python scripts/pdca_bridge_runner.py `
  --kind output `
  --input-file C:\tmp\review-output.md `
  --reviewer sc-ifr `
  --runtime codex `
  --mode review-only `
  --repo-root C:/path/to/actual-target-repo `
  --forward-to-pdca
```

その後にもう一度 `prepare-implementation-context.py` を実行する。

---

## 詰まりやすい点

### 1. `REVIEW_FEEDBACK_SCRIPT` 未設定

この repo 単体では producer を完全同梱していない。

少なくとも今は:
- `review-fix-pipeline/scripts/review-feedback.py`
or
- `~/.claude/scripts/review-feedback.py`

のどちらかが必要。

### 2. `--cwd` が target repo を向いていない

再注入は repo scope に依存する。  
**対象 repo の root を `--cwd` で明示**する。

### 3. finding がまだ 0 件

その場合は context が空でも正常。  
まず `review-fix-pipeline` 側の bridge で 1 件作ってから再実行する。

---

## 次の段階

この quickstart が通ったら、次に見るのは:

1. Claude hook mode
2. Codex/manual mode
3. `record-review-outcome.py` を含む producer flow

詳細は:
- `docs/hooks.md`
- `docs/auto-pdca-producer-design.md`

を参照。

---

## 今後の予定

将来的にはここに追加したい:

- bootstrap script の本格化
- sample repo を使った end-to-end quickstart
- producer 同梱 or vendor 方針の明確化
