# dismissed 設計方針・安全ルール

## 核心原則: Claude 自身による dismissed 処理は禁止

### なぜ禁止か

Claude がレビュー findings を「関係ない」「false positive だ」と自己判断して dismissed にするのは
**シンプル化トラップ（Sycophancy Trap）** に直結する。

```
問題のある流れ:
Claude がコードを書く
    → /ifr で findings が検出される
    → Claude が「この finding は自分のコードには当てはまらない」と判断
    → dismissed にする
    → 実は重要な問題だったが握り潰される
```

これは「コードを書いた本人が自分へのレビューを裁定する」状況と同じ。
評価者と被評価者が同一人物になるため、客観性が保てない。

---

## 許可される dismissed 操作

### ケース1: ユーザーが明示的に承認した場合（唯一の原則）

```bash
# ユーザーが「これは false positive だ」と明示的に指示した場合のみ
python review-feedback.py dismiss --id <finding_id> --reason "理由"
```

dismissed_by = "user" として記録する。
Claude が代理で実行することは禁止。

### ケース2: 完全重複の自動スキップ（注入時のみ・DB は変更しない）

同一ファイル、同一カテゴリ、同一 summary が既に `resolution = 'fixed'` の場合、
**注入をスキップするだけ**（dismissed フラグは立てない）。

```sql
-- 注入クエリに追加する重複チェック
AND NOT EXISTS (
    SELECT 1 FROM review_feedback r2
    WHERE r2.file_path = review_feedback.file_path
      AND r2.category  = review_feedback.category
      AND r2.summary   = review_feedback.summary
      AND r2.resolution = 'fixed'
)
```

これは DB を変更しない。あくまで「今回は注入しない」だけ。

---

## dismissed の記録フォーマット

| カラム | 許可値 | 説明 |
|--------|--------|------|
| dismissed | 0 / 1 | 1 = dismissed |
| dismissed_by | "user" のみ | "claude" "auto" 禁止 |
| dismissed_at | TIMESTAMP | 承認日時 |
| fp_reason | TEXT | false positive の理由（任意） |

---

## 学習ループへの昇格条件

dismissed finding が CLAUDE.md に「学習済みパターン」として追記されるのは:

1. dismissed_by = "user"（必須）
2. 同一 category + fp_reason の組み合わせが **2回以上** ユーザーに承認された
3. SessionEnd hook の集計バッチで自動追記

1回だけの dismissed は「今回だけ例外」として扱い、CLAUDE.md には書かない。
繰り返し承認されることで初めて「学習すべきパターン」と判定する。

---

## Claude が dismissed を提案する場合のルール

Claude が「これは false positive かもしれない」と感じた場合、
**実行せず提案だけする**。

```
Claude の正しい振る舞い:
「この finding (#42: bridge/mail-sender.py の SMTP タイムアウト未設定) は
 現在の実装では意図的な設計のため false positive の可能性があります。
 dismiss しますか？　→ yes なら python review-feedback.py dismiss --id 42 --reason "..." を実行します」
```

ユーザーの「yes」を待ってから実行する。
