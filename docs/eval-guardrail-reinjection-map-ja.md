# PDCA Reinjection Map for Eval / Guardrail Design

この文書は、`review-fix-pipeline/docs/eval-guardrail-design-playbook-ja.md` を `claude-review-pdca` 側の実装に対応づけるためのメモである。

---

## 役割分担

| repo | 役割 |
|---|---|
| `review-fix-pipeline` | レビュー意味論、safe fix / judgment call / critical verification / outcome contract |
| `claude-review-pdca` | findings 永続化、実装前再注入、HITL dismissal、rule promotion |

一言で言うと、`review-fix-pipeline` が **Check の意味**を定義し、`claude-review-pdca` が **Plan / Act の記憶と再利用**を担当する。

---

## 設計段階の eval / guardrail と実装対応

| 設計段階で決めること | `claude-review-pdca` の対応 |
|---|---|
| 過去 findings をいつ戻すか | `PreToolUse` / `prepare-implementation-context.py` |
| どの findings を戻すか | file_path / repo_root / severity / freshness / resolution filter |
| ノイズをどう抑えるか | LIMIT、session dedup、stale除外、info/nitpick除外 |
| false positive をどう扱うか | dismissed は user approval 必須 |
| 学習をどこに保存するか | `review-feedback.db` / `review-patterns.db` |
| 再利用価値のあるものをどうルール化するか | `rule-promotion-design.md` の HITL rule promotion |
| Claude hooks がない環境でどう使うか | Codex/manual mode の `prepare-implementation-context.py` |

---

## 企業向けの説明

```text
レビュー結果を保存するだけではなく、次回の実装前に対象ファイルへ関係する findings だけを戻す設計にしています。
全件を注入するとノイズになるため、repo scope、file path、severity、freshness、resolution で絞ります。
また、false positive の dismiss や repo rule への昇格は自動化せず、人間承認に残しています。
```

---

## 面接で強調する順番

1. **file-specific reinjection**  
   全部を読ませるのではなく、対象ファイルに関係する失敗だけ戻す。

2. **SNR control**  
   severity / freshness / stale / dedup でノイズを抑える。

3. **HITL dismissal**  
   AIが自分に不都合な finding を勝手に dismissed しない。

4. **rule promotion**  
   繰り返し発生するものだけ、人間承認で `CLAUDE.md` / `CODEX.md` に昇格する。

5. **runtime portability**  
   Claude Code hook でも Codex/manual command でも同じ概念を使える。

---

## `review-fix-pipeline` との接続説明

```text
review-fix-pipeline がレビュー結果を structured outcome に変換し、claude-review-pdca がそれを保存・再注入します。
これにより、AIレビューが一回限りの品質チェックではなく、次回実装に効く PDCA loop になります。
```

---

## 追加すると強い evidence

今後、企業向け証拠として強くするなら以下を足す。

- サンプル findings DB と before/after injection 例
- `prepare-implementation-context.py` の実行例スクリーンショット
- `review outcome -> DB -> reinjection -> rule promotion` の1枚図
- false positive を user approval なしでは dismiss しないテストケースの説明
- Codex/manual mode の利用例
