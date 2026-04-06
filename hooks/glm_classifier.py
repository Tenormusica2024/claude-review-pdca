"""
GLM-5.1 による finding カテゴリ自動分類モジュール。

レビュー findings の summary テキストから13カテゴリ enum に分類する。
OpenRouter API 経由で GLM-5-turbo を呼び出し、JSON で結果を返す。

コスト最適化: Opus（オーケストレーション）ではなく GLM（軽量・安価）で分類を実行。
"""

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Optional

# 13カテゴリ enum（pattern_db.py と同一）
VALID_CATEGORIES = frozenset({
    "logic", "security", "robustness", "data-integrity", "concurrency",
    "type-safety", "performance", "api-contract", "test-quality",
    "consistency", "documentation", "ux", "maintainability",
})

# OpenRouter API 設定
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
GLM_MODEL = "z-ai/glm-5-turbo"
MAX_RETRIES = 2
REQUEST_TIMEOUT = 15

# 分類プロンプト（JSON出力を強制）
CLASSIFICATION_PROMPT = """あなたはコードレビュー findings の分類エキスパートです。
以下の finding summary を読み、最も適切なカテゴリを1つ選んでください。

## カテゴリ一覧（必ずこの中から1つだけ選ぶこと）
- logic: ロジックバグ、off-by-one、条件分岐の誤り
- security: セキュリティ脆弱性、バリデーション不足、データ漏洩リスク
- robustness: エラーハンドリング不足、リソース管理、エンコーディング問題
- data-integrity: データ整合性、クエリ正確性、データ品質
- concurrency: 並行処理、状態管理、レースコンディション
- type-safety: 型安全性、null安全性
- performance: パフォーマンス問題、不要な計算、メモリ効率
- api-contract: API設計、アーキテクチャ、設定、依存関係
- test-quality: テスト品質、カバレッジ、テスタビリティ
- consistency: 命名規則、スタイル、コード重複、デッドコード
- documentation: ドキュメント、コメント、可読性
- ux: アクセシビリティ、i18n、ナビゲーション
- maintainability: コード品質、複雑性、保守性

## 出力形式（JSON のみ。説明文は不要）
{{"category": "カテゴリ名", "confidence": 0.0-1.0}}

## Finding summary:
{summary}"""


def _get_api_token() -> Optional[str]:
    """OpenRouter API トークンを取得する。"""
    return os.environ.get("ZAI_AUTH_TOKEN")


def classify_finding(
    summary: str,
    severity: str = "warning",
    file_path: Optional[str] = None,
) -> dict:
    """
    Finding summary を13カテゴリに分類する。

    Args:
        summary: finding のサマリーテキスト
        severity: finding の severity（分類のヒントとして使用）
        file_path: 対象ファイルパス（分類のヒントとして使用）

    Returns:
        {"category": str, "confidence": float, "source": "glm"|"fallback"}
    """
    token = _get_api_token()
    if not token:
        # API トークンなし → validate_category によるルールベースフォールバック
        return _fallback_classify(summary)

    # コンテキストを付加してプロンプト構成
    context_hint = ""
    if file_path:
        context_hint += f"\nファイル: {file_path}"
    if severity:
        context_hint += f"\nSeverity: {severity}"

    prompt_text = CLASSIFICATION_PROMPT.format(summary=summary + context_hint)

    for attempt in range(MAX_RETRIES + 1):
        try:
            result = _call_glm_api(token, prompt_text)
            if result and result.get("category") in VALID_CATEGORIES:
                result["source"] = "glm"
                return result
            # カテゴリが不正 → リトライ
            if attempt < MAX_RETRIES:
                continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[glm_classifier] API error (attempt {attempt + 1}): {e}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                continue

    # 全リトライ失敗 → フォールバック
    return _fallback_classify(summary)


def _call_glm_api(token: str, prompt: str) -> Optional[dict]:
    """OpenRouter API 経由で GLM を呼び出し、JSON レスポンスを返す。"""
    payload = json.dumps({
        "model": GLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 100,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    body = json.loads(resp.read().decode("utf-8"))

    content = body["choices"][0]["message"]["content"]
    # JSON 抽出（GLM が余分なテキストを付加する場合への対策）
    return _extract_json(content)


def _extract_json(text: str) -> Optional[dict]:
    """テキストから JSON オブジェクトを抽出する。"""
    # まず全体をパース
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # { ... } を探してパース
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _fallback_classify(summary: str) -> dict:
    """API 不可時のルールベース分類（pattern_db.validate_category のキーワードマッチ）。"""
    lower = summary.lower()

    # キーワードベースの簡易分類
    keyword_map = [
        (["sql injection", "xss", "csrf", "auth", "token", "credential", "secret", "leak"], "security"),
        (["test", "mock", "assert", "coverage", "fixture"], "test-quality"),
        (["off-by-one", "wrong condition", "logic error", "incorrect", "bug", "edge case"], "logic"),
        (["try", "except", "error handling", "exception", "resource", "cleanup", "encoding"], "robustness"),
        (["race", "deadlock", "thread", "async", "concurrent", "lock", "mutex"], "concurrency"),
        (["type", "null", "none", "undefined", "cast", "coerce"], "type-safety"),
        (["slow", "memory", "cache", "o(n", "o(n²", "performance", "latency"], "performance"),
        (["api", "contract", "interface", "config", "dependency", "import"], "api-contract"),
        (["data", "integrity", "consistency", "migration", "schema", "query"], "data-integrity"),
        (["naming", "style", "duplicate", "dead code", "unused"], "consistency"),
        (["doc", "comment", "readme", "docstring"], "documentation"),
        (["accessibility", "a11y", "i18n", "ux", "ui"], "ux"),
    ]

    for keywords, category in keyword_map:
        if any(kw in lower for kw in keywords):
            return {"category": category, "confidence": 0.5, "source": "fallback"}

    return {"category": "maintainability", "confidence": 0.3, "source": "fallback"}


def classify_findings_batch(
    findings: list[dict],
    max_batch: int = 20,
) -> list[dict]:
    """
    複数 findings を一括分類する。

    Args:
        findings: [{"summary": str, "severity": str, "file_path": str, ...}, ...]
        max_batch: 最大処理件数

    Returns:
        入力と同じ構造に "classified_category" と "classification_confidence" を追加したリスト
    """
    results = []
    for i, f in enumerate(findings[:max_batch]):
        summary = f.get("summary", "")
        if not summary:
            f["classified_category"] = "maintainability"
            f["classification_confidence"] = 0.0
            f["classification_source"] = "skip"
            results.append(f)
            continue

        classification = classify_finding(
            summary=summary,
            severity=f.get("severity", "warning"),
            file_path=f.get("file_path"),
        )
        f["classified_category"] = classification["category"]
        f["classification_confidence"] = classification.get("confidence", 0.5)
        f["classification_source"] = classification.get("source", "unknown")
        results.append(f)

    return results
