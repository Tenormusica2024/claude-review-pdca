# claude-review-pdca 公開可否・セキュリティ確認メモ

作成日: 2026-04-30
対象 repo: `C:\Users\Tenormusica\claude-review-pdca`

## 結論

条件付きでポートフォリオ掲載可能です。

この repo は、AIコードレビューの指摘を保存・再注入する仕組みそのものが説明価値の中心です。一方で、実際のレビューDBや個別repoの指摘内容には、未公開実装・作業方針・ローカルパス・秘密情報の痕跡が混ざる可能性があるため、公開対象は設計説明と抽象化した利用例に限定する必要があります。

## 公開してよい内容

- AIコードレビュー指摘を蓄積し、次回実装へ再注入する設計思想
- SQLite を使った findings 管理の概要
- repo / file / category / severity のような抽象的なメタデータ構造
- Claude hook mode と Codex/manual mode の両対応という実行形態
- 編集回数・セッション終了時の batch review 方針
- AIによる自己dismissを避ける人間承認ポリシー
- 繰り返し指摘を repo ルールに昇格する HITL workflow
- `public-portfolio-summary-ja.md` に記載した抽象説明・30秒説明・タイトル案

## 公開しない方がよい内容

- 実際の `review-feedback.db` の中身
- 個別repo・個別ファイルに紐づく未公開レビュー指摘
- raw finding / raw review output / セッションログ全文
- ユーザー固有のローカルパスを含む実データ
- クライアント名・業務名・未公開repo名が入ったレビュー事例
- API key、token、cookie、認証情報、秘密のenv値
- 秘密情報が混ざった可能性のある dismiss / false-positive 学習履歴

## llmwiki 候補理由への判断

`portfolio_review_candidates.json` では、この repo は以下の理由で候補化されていました。

- `baseline-stale`
- `readiness=needs-polish-before-pin`
- `security=medium`

今回の作業では、公開説明の粒度を明確化し、ポートフォリオで説明できる単位を追加しました。これにより `readiness=needs-polish-before-pin` の主要因である差別化説明不足は軽減されています。

`security=medium` は、現時点の候補データ上では `docs/auto-pdca-producer-design.md` と `docs/rule-promotion-design.md` にある credential 関連の記述検知です。これは設計上の注意・一般語の検知である可能性が高く、即時に実シークレット露出を意味するものではありません。ただし、公開時は raw finding や実DBを出さない運用が必須です。

## 実施した安全確認

- 作業前の `git status --short` は clean
- 公開説明は新規 docs に限定し、既存の実装・hook・DB処理には触れていない
- 実レビューDBや個別 finding の内容は本文に含めていない
- 公開説明は repo の設計・運用思想・抽象的な活用場面に限定した

## 残る注意点

ポートフォリオへ出す場合は、READMEやdocs全文をそのまま転載するのではなく、`public-portfolio-summary-ja.md` の短い説明をベースにしてください。特に、実DB・実レビュー結果・ローカルパス・未公開repo名が混ざる出力は公開対象外です。

## 次の一手

- この repo を公開候補として扱う場合は、B2B/portfolio側に掲載する前にユーザー本人が文面を確認する
- 実際のDBサンプルを見せたい場合は、完全に架空の toy example を別途作る
- llmwiki 側では、このレビュー結果を反映した baseline 更新を repo 単位で行う
