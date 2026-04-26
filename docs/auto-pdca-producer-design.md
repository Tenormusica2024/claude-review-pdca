# Auto PDCA Producer 設計

## 目的

`sc-rfl` / `sc-ifr` / `sc-ir` 実行後に出たレビュー結果を、
次回の実装で再利用できる形に**自動または半自動で永続化**する。

現状の `claude-review-pdca` は主に **consume 側**（過去 findings / learned patterns の再注入）は整っているが、
**producer 側**（今回のレビュー結果を DB に戻す部分）は未接続または reviewer ごとに不統一である。

この設計の目的は、以下を共通化すること:

1. `sc-rfl` / `sc-ifr` / `sc-ir` の出力を同じ入口で受ける
2. unresolved finding と learned pattern を**保存先別に分離**して記録する
3. false positive 学習は HITL を維持しつつ、safe fix / pending / judgment call の PDCA を自動化する

---

## スコープ

### 対象 reviewer / skill

- `sc-rfl`
- `sc-review-fix-loop`
- `sc-ifr`
- `sc-ir`
- 将来の reviewer alias:
  - `/review-fix-loop`
  - `/rfl`
  - `/intent-first-review`
  - `ifr`

### 対象データ

- review で検出された unresolved findings
- review 中に safe fix 済みだったが、**再利用価値のある実装パターン**
- user judgment が必要な要確認項目
- false positive 候補（ただし自動学習しない）

### 非スコープ

- 一般会話や brainstorm の雑多な指摘の自動保存
- style-only / taste-only / 一回限りの wording 修正の学習
- false positive の自動 dismiss
- architecture / UX / API policy を AI が勝手に確定して記録すること

---

## 現状整理

## repo 責務分離

この機能は 1 repo で完結しない。責務を以下に分ける。

### `claude-review-pdca`

役割:
- PDCA persistence / reinjection 基盤
- `review-feedback.db` / `review-patterns.db` への分流
- implementation context bridge
- producer / consumer contract の保存側実装

ここで管理するもの:
- `record-review-outcome.py` のような producer 本体
- `prepare-implementation-context.py`
- findings / learned patterns の注入ロジック

### `review-fix-pipeline`

役割:
- `/ifr` `/rfl` `/go-robust` 相当の review semantics の正本
- review result を structured outcome に正規化する source of truth
- Claude Code / Codex それぞれの review runtime adapter

ここで管理するもの:
- reviewer 名
- output contract に出す item の意味
- safe fix / judgment call / unresolved の判定意味論

### 分離の原則

- review の**意味**は `review-fix-pipeline`
- review の**保存と再注入**は `claude-review-pdca`

この境界を崩さない。

### できていること

1. `review-feedback.db`
   - pending findings の保存先
   - PreToolUse で file-specific reinjection

2. `review-patterns.db`
   - learned implementation patterns の保存先
   - implementation gate が立ったときだけ注入

3. Codex / manual bridge
   - `scripts/prepare-implementation-context.py`
   - hook 非対応環境でも PreToolUse 相当を再現

### できていないこと

1. `sc-rfl` / `sc-ifr` / `sc-ir` の結果を**共通形式で受ける producer**
2. review 出力から
   - safe fix
   - unresolved finding
   - judgment call
   - false positive candidate
   を機械可読で抽出する処理
3. その抽出結果を
   - `review-feedback.db`
   - `review-patterns.db`
   に分流する処理

---

## 設計方針

## 1. consumer と producer を明確に分離する

- **consumer**: 過去知見を次回実装で再注入する
- **producer**: 今回のレビュー結果を次回に使える形で保存する

この設計書は **producer 側**の責務を定義する。

## 1.5. 共通 contract + runtime adapter 分離

Claude Code と Codex は execution model が違うが、
review findings の意味まで runtime ごとに分岐させない。

守る方針:

1. **review semantics は共通**
   - severity
   - auto_fixable
   - needs_judgment
   - status
   - category

2. **runtime 差分は adapter で吸収**
   - Claude Code adapter: slash command / hook / Agent 前提
   - Codex adapter: skill / shell / PowerShell / manual bridge 前提

3. **producer は runtime 非依存の payload を受ける**
   - `sc-rfl` 由来でも
   - `sc-ifr` 由来でも
   - Claude runtime でも Codex runtime でも
   同じ schema に正規化してから保存する

つまり:
- branch を Claude 用 / Codex 用で恒久分岐しない
- contract は 1 本
- adapter は複数本

## 2. 保存先を 2 系統に分離する

### A. `review-feedback.db`
用途:
- 次回も unresolved として参照すべき findings
- 人間があとで resolve / dismiss / accept する対象

入れるもの:
- 未修正の bug / robustness issue / security issue
- 次回実装で再注入したい pending finding
- judgment required だが issue として覚えておく価値があるもの

入れないもの:
- safe fix 済みで、その場で閉じたもの
- style-only 指摘
- false positive と断定できない曖昧ノイズ

### B. `review-patterns.db`
用途:
- 「この reviewer はこういう修正をよく入れる」という implementation memory
- implementation gate 下でだけ注入する learned patterns

入れるもの:
- safe fix 済みで、再発しやすく、再注入価値が高いもの
- unresolved でも pattern 化価値が高いもの（warning 以上）

入れないもの:
- user judgment 依存が強いもの
- repo / file と結びつかない抽象論
- style-only / wording-only

## 3. false positive 学習は引き続き HITL

producer 自動化を入れても、以下は人間だけが決める:

- `dismissed = 1`
- `dismissed_by = 'user'`
- `fp_reason`
- `CLAUDE.md` への FP パターン昇格

つまり:

- **実装修正パターンの学習**は自動化してよい
- **誤検知学習**は自動化しない

## 4. reviewer ごとの差ではなく、共通の output contract を作る

`sc-rfl` / `sc-ifr` / `sc-ir` はレビューの重さやループ数が違うが、
producer には共通の構造で渡す。

---

## 正規化 reviewer 名

producer 側では以下に正規化する。

| 入力 | 正規化 reviewer |
|---|---|
| `sc-rfl` | `review-fix-loop` |
| `sc-review-fix-loop` | `review-fix-loop` |
| `/review-fix-loop` | `review-fix-loop` |
| `/rfl` | `review-fix-loop` |
| `sc-ifr` | `intent-first-review` |
| `ifr` | `intent-first-review` |
| `/intent-first-review` | `intent-first-review` |
| `sc-gr` | `go-robust` |
| `/go-robust` | `go-robust` |
| `sc-ir` | `intent-review-light` |

補足:
- `sc-ifr` は `/ifr` 相当なので `intent-first-review` 系に寄せる
- `sc-ir` は軽量版として別 reviewer 名にして残す
  - 理由: 同じ intent 系でも検出密度・修正強度が違うため、pattern quality の観測を分けたい

---

## 共通 output contract

producer が受け取る中間形式:

```json
{
  "schema_version": 1,
  "session_id": "sess-123",
  "repo_root": "C:/repo",
  "reviewer": "intent-first-review",
  "mode": "normal",
  "target": {
    "kind": "files",
    "files": ["src/app/main.py", "src/lib/util.py"]
  },
  "items": [
    {
      "type": "finding",
      "title": "missing null guard before dereference",
      "summary": "calling result.value without checking result is None can crash on empty response",
      "severity": "warning",
      "category": "robustness",
      "file_path": "src/app/main.py",
      "line": 118,
      "status": "pending",
      "auto_fixable": false,
      "needs_judgment": false,
      "confidence": "high"
    },
    {
      "type": "finding",
      "title": "quoted shell invocation for python helper",
      "summary": "shell-quoted subprocess call is fragile on paths with quotes or shell metacharacters",
      "severity": "warning",
      "category": "robustness",
      "file_path": "hooks/review-feedback-session-check.js",
      "line": 78,
      "status": "fixed",
      "auto_fixable": true,
      "needs_judgment": false,
      "confidence": "high"
    },
    {
      "type": "judgment_call",
      "title": "whether to persist lightweight sc-ir findings into pending queue",
      "summary": "low-cost critique may become noise if all sc-ir items are promoted",
      "severity": "info",
      "category": "maintainability",
      "file_path": "docs/auto-pdca-producer-design.md",
      "line": null,
      "status": "judgment-required",
      "auto_fixable": false,
      "needs_judgment": true,
      "confidence": "medium"
    }
  ],
  "verification": {
    "commands": ["pytest -q"],
    "summary": "178 passed, 1 skipped"
  }
}
```

---

## producer の責務

共通 producer（仮名: `scripts/record-review-outcome.py`）は以下を行う。

### 1. 入力受理

受理方法:
- `--payload-json`
- `--payload-file`

入力ソース:
- `sc-rfl` wrapper
- `sc-ifr` wrapper
- `sc-ir` wrapper
- 将来の Claude hook / review command bridge

### 2. reviewer 正規化

- alias を正規 reviewer 名へ正規化
- repo_root / file_path を slash 形式に正規化

### 3. item 分類

各 item を以下に再分類する:

---

## 実運用で確認できたこと

`gittrend-jp` を使った live-run で、少なくとも以下は確認できた。

### `/ifr` 系

- pending finding は `review-feedback.db` に入る
- 次回 implementation gate で **直ちに reinjection** される
- ここでは learned pattern を急がず、まず feedback を効かせる設計でよい

### `review-fix-loop` 系

- fixed finding は `review-patterns.db` に入る
- ただし learned pattern 注入は **cool-off (`detection_count >= 2`)** を満たしてから行う
- そのため、1回目の safe fix 直後は pattern が見えなくても正常

### learned pattern 注入

- pattern は file-specific に注入される
- `README.md` の learned pattern は `README.md` 編集時にだけ見える
- 逆に、同じ repo 内でも別ファイルには出ない

### taxonomy

- 実運用では reviewer 側カテゴリに `ci` / `onboarding` のような実務ラベルが出やすい
- pattern 側 taxonomy では alias を持たせ、
  - `ci` → `test-quality`
  - `onboarding` → `documentation`
  のように **早めに正規化** した方がよい

### `/ifr` markdown bridge の file_path

- legacy markdown 由来の `/ifr` pending は、free-text だけだと `file_path` が落ちることがある
- bridge 側で
  - 本文の path-like token
  - `--target-file`
  - title / summary の弱いヒント
  を使う推定を入れると、`install.ps1` や `docs/quickstart-from-fork.md` のような file-specific finding をかなり回収できる
- ただし 1 本の markdown に複数ファイルの finding を混在させると誤寄せの余地が残るので、
  中長期的には **structured output / explicit target** に寄せる方がよい

- `feedback_pending`
- `feedback_fixed`
- `pattern_candidate`
- `judgment_pending`
- `ignore`

### 4. 保存先へ分流

- `review-feedback.py record`
- `record-rfl-patterns.py` または共通化後継 script

### 5. 結果サマリ出力

```json
{
  "recorded_feedback": 3,
  "recorded_patterns": 2,
  "judgment_items": 1,
  "ignored_items": 4
}
```

---

## 分流ルール

## A. `review-feedback.db` に入れる条件

以下を満たす item は pending finding として保存候補:

1. `type = finding`
2. `status in ('pending', 'judgment-required')`
3. `severity in ('critical', 'high', 'warning')`
4. `confidence in ('high', 'medium')`
5. style-only / doc-only / taste-only ではない

補足:
- `judgment-required` でも bug / robustness / security の文脈なら pending に残す
- `info` は原則入れない

### `review-feedback.db` への resolution 初期値

| item status | 保存時 resolution |
|---|---|
| `pending` | `pending` |
| `judgment-required` | `pending` |
| `fixed` | 原則保存しない |

備考:
- judgment call 専用 resolution を増やす案はあるが、初版では増やさない
- 代わりに `project` / `summary` / `reviewer` / メタに残す

## B. `review-patterns.db` に入れる条件

以下を満たす item は pattern candidate:

1. `type = finding`
2. `severity in ('critical', 'high', 'warning')`
3. `confidence = 'high'`
4. 再発しやすいローカル実装問題
5. 次回 implementation で再注入価値がある

特に:
- `status = fixed` の safe fix は強い candidate
- `status = pending` でも pattern 化価値があれば記録してよい
  - ただし cool-off により初回即注入はしない

## C. ignore する条件

以下は producer で保存しない:

- taste-only
- wording-only
- architecture proposal only
- product / UX 方針そのもの
- confidence が低く cheap verification もできていないもの
- file_path を全く特定できない雑多なコメント

---

## skill ごとの扱い

## 1. `sc-rfl`

性質:
- review + safe fix loop
- safe fix が多い
- iteration あり

producer 方針:
- safe fix 済み item は `review-patterns.db` に積極記録
- unresolved は `review-feedback.db`
- loop 最終時点の unresolved を authoritative とする

## 2. `sc-ifr`

性質:
- thorough review
- safe autonomous fixes あり
- 要確認 handling あり

producer 方針:
- `sc-rfl` とほぼ同じ
- ただし reviewer は `intent-first-review`
- safe fix と unresolved の両方を扱う
- **pending はまず feedback 側を主に使い、pattern 学習は急がない**
- pattern 側へ送るのは、少なくとも `fixed` になった item を優先する

## 2.5 `sc-gr` / `/go-robust`

性質:
- `/ifr` や `/rfl` で残った要確認を policy ベースで処理する
- fix の意味は「単なる review 指摘」ではなく「judgment 解消後の実装」

producer 方針:
- reviewer は `go-robust`
- **fixed になった resolved item は pattern source として有用**
- unresolved judgment item は feedback 側には残してよいが、pattern 学習は急がない

## 3. `sc-ir`

性質:
- lightweight review
- critique 密度は低め
- ノイズ混入リスクがある

producer 方針:
- **全件を自動保存しない**
- 保存対象を warning 以上 / confidence 高め / file 特定あり に制限
- safe fix を実際に入れた場合のみ pattern candidate として優先

つまり `sc-ir` だけはやや stricter にする。

---

## 自動化境界

## 自動でやる

1. reviewer 正規化
2. file_path / repo_root 正規化
3. safe fix 済み item の pattern candidate 化
4. unresolved warning 以上の pending 化
5. repo-local / runtime-local ログ出力

## 自動でやらない

1. false positive dismiss
2. user judgment が必要な policy 確定
3. architecture / API 方針の勝手な学習
4. low-confidence finding の自動 pending 化

---

## 実行タイミング

## Claude runtime

理想:

1. review command 実行
2. review 結果を structured payload 化
3. review 終了時に producer script 呼び出し

初期は wrapper / 手動連携でもよい。

## Codex runtime

理想:

1. `sc-rfl` / `sc-ifr` / `sc-ir` 完了
2. skill 実装内またはその直後に producer script 呼び出し

補足:
- 現在の Codex は hook がないため、review 実行ラッパーまたは post-step 明示呼び出しが必要

---

## 推奨アーキテクチャ

```
sc-rfl / sc-ifr / sc-ir
    │
    ├─ safe fix 実行
    ├─ unresolved / 要確認 / fixed を整理
    ▼
structured review outcome payload
    ▼
record-review-outcome.py   ← 新規共通 producer (`claude-review-pdca`)
    │
    ├─ normalize reviewer / path / severity / category
    ├─ split: feedback_pending / pattern_candidate / ignore
    │
    ├─ review-feedback.py record
    └─ record-review-patterns.py   （既存 record-rfl-patterns.py の一般化候補）
```

補足:
- payload を作る責務は `review-fix-pipeline`
- payload を保存先へ分流する責務は `claude-review-pdca`

---

## 既存 script の扱い

## `record-rfl-patterns.py`

現状:
- 名前が RFL 専用に見える
- 実態は pattern DB 記録 bridge

方針:
- 初期は使い回してよい
- ただし将来的には以下へ rename / wrap 推奨

候補:
- `record-review-patterns.py`
- `record-implementation-patterns.py`

理由:
- `sc-ifr` / `sc-ir` でも同じ処理にしたいから

---

## judgment call の扱い

judgment call は 2 種類に分ける。

### A. 再注入価値のある judgment call

例:
- API 互換性維持のため null を許可するか
- public behavior を変えるか

扱い:
- `review-feedback.db` に pending として残してよい
- ただし severity は warning 以上

### B. 一回限りの相談事項

例:
- 今回 rename するかどうか
- 今回の README 文体をどっちにするか

扱い:
- DB 保存しない
- review 出力にだけ残す

---

## dedup / ノイズ抑制

producer 側でも dedup が必要。

## dedup key 候補

- `repo_root`
- `normalized reviewer`
- `file_path`
- `category`
- `normalized summary hash`

## 抑制ルール

1. 直近 open pending とほぼ同一なら再記録しない
2. safe fix 済み pattern は `record_pattern()` 側 upsert に寄せる
3. `sc-ir` 由来の warning 未満は保存しない
4. `maintainability` デフォルト分類は条件を厳しめにする

---

## 失敗時フォールバック

## parse 失敗

- producer は落とさず warning を出す
- review 自体は成功扱い
- payload を `logs/review-outcome-deadletter/` 的な退避先に落とす案あり

## DB 書き込み失敗

- `review-feedback` と `review-patterns` は分離して扱う
- 片方が失敗しても片方は継続
- 結果サマリに partial failure を出す

## category 不明

- `--classify` で GLM 分類
- それでも曖昧なら `maintainability`
- ただし `sc-ir` の `maintainability + info` は原則保存しない

---

## ロールアウト段階

## Phase 1: 設計固定

- 本設計書を作る
- reviewer 正規化表を決める
- output contract を決める

## Phase 2: 共通 producer 初版

- `record-review-outcome.py` 追加
- `record-rfl-patterns.py` の再利用 or wrap
- JSON payload 入力で分流

## Phase 3: skill 側接続

- `sc-rfl` 後に producer 呼び出し
- `sc-ifr` 後に producer 呼び出し
- `sc-ir` 後に stricter ルールで producer 呼び出し

## Phase 4: structured output 化

- review skill 出力から安定して payload を抽出できるようにする
- できれば free-text parse 依存を減らす

## Phase 5: review-fix-pipeline integration

- `review-fix-pipeline` 側で structured review outcome を正式 contract 化
- `sc-rfl` / `sc-ifr` / `sc-ir` から共通 producer 呼び出しへ接続
- Claude adapter / Codex adapter の差分を明示化

---

## 未決事項

1. `sc-ir` を `intent-first-review` に寄せるか別 reviewer にするか
   - 現時点では別名 `intent-review-light` 推奨

2. judgment call 専用 resolution を DB に増やすか
   - 初版は増やさず `pending` で扱う

3. `review-feedback.py record` の I/O 契約を拡張するか
   - 初版は wrapper 側変換で吸収

4. review 出力を free-text から抽出するか、skill 側で JSON block を必ず出させるか
   - 中長期では **skill 側 structured output 必須化** が望ましい

---

## 推奨結論

次に実装すべきは:

1. `record-review-outcome.py` を新規追加
2. `sc-rfl` / `sc-ifr` / `sc-ir` 共通 payload を受ける
3. `review-feedback.db` と `review-patterns.db` に分流
4. `sc-ir` だけ stricter 保存ルールにする
5. false positive は従来どおり HITL のままにする
6. `review-fix-pipeline` では contract を 1 本に保ち、Claude/Codex 差分は adapter 層に閉じ込める

これで

- `sc-rfl` の自動修正可 / 要確認
- `sc-ifr` の自動修正可 / 要確認
- `sc-ir` の軽量レビュー結果

を **同じ PDCA producer サイクル** に乗せられる。
