"""
GLM-5.1 による finding カテゴリ自動分類モジュール。

レビュー findings の summary テキストから13カテゴリ enum に分類する。
Z.ai の Anthropic 互換 API 経由で GLM を呼び出し、JSON で結果を返す。

コスト最適化: Opus（オーケストレーション）ではなく GLM（軽量・安価）で分類を実行。
"""

import json
import os
import sys
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from config import (
    ANTHROPIC_VERSION,
    GLM_API_URL,
    GLM_FALLBACK_LOG_PATH,
    GLM_HTTP_429_SUPPRESSION_THRESHOLD,
    GLM_MODEL,
    GLM_SUPPRESSION_LOOKBACK,
)

# 13カテゴリ enum（pattern_db.py と同一）
VALID_CATEGORIES = frozenset({
    "logic", "security", "robustness", "data-integrity", "concurrency",
    "type-safety", "performance", "api-contract", "test-quality",
    "consistency", "documentation", "ux", "maintainability",
})

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
    """Z.ai API トークンを取得する。"""
    return os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ZAI_AUTH_TOKEN")


def _should_retry_api_error(error: Exception) -> bool:
    """再試行価値の薄い API エラーを除外する。"""
    if isinstance(error, urllib.error.HTTPError):
        return error.code not in {401, 403, 429}
    return True


def _describe_api_error(error: Exception) -> str:
    """観測用の簡易エラー種別に正規化する。"""
    if isinstance(error, urllib.error.HTTPError):
        return f"http_{error.code}"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, json.JSONDecodeError):
        return "json_decode_error"
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", None)
        if isinstance(reason, TimeoutError):
            return "timeout"
        return "url_error"
    return "unknown_error"


def _build_fallback_result(summary: str, reason: str) -> dict:
    """ルールベース分類に観測用 fallback 理由を付与する。"""
    result = _fallback_classify(summary)
    result["fallback_reason"] = reason
    return result


def _append_fallback_event(
    summary: str,
    severity: str,
    file_path: Optional[str],
    reason: str,
    source: str,
    reviewer: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> None:
    """fallback 観測イベントを append-only JSONL に記録する。"""
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("GLM_CLASSIFIER_ENABLE_TEST_LOGGING") != "1":
        return

    try:
        GLM_FALLBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "source": source,
            "severity": severity,
            "reviewer": reviewer,
            "repo_root": repo_root.replace("\\", "/") if repo_root else None,
            "file_path": file_path.replace("\\", "/") if file_path else None,
            "summary_hash": hashlib.sha1(summary.encode("utf-8")).hexdigest()[:12],
            "summary_preview": summary[:120],
        }
        with GLM_FALLBACK_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[glm_classifier] fallback log write failed: {e}", file=sys.stderr)


def _normalize_scope_value(value: Optional[str]) -> str:
    """repo_root/file_path 比較用にパスを正規化する。"""
    return value.replace("\\", "/").rstrip("/") if value else ""


def _load_recent_fallback_events(
    repo_root: Optional[str] = None,
    reviewer: Optional[str] = None,
    limit: int = GLM_SUPPRESSION_LOOKBACK,
) -> list[dict]:
    """JSONL 末尾から直近イベントを読み、必要なら scope で絞る。"""
    default_log_path = Path.home() / ".claude" / "logs" / "glm-classifier-fallbacks.jsonl"
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("GLM_CLASSIFIER_ENABLE_TEST_LOGGING") != "1"
        and GLM_FALLBACK_LOG_PATH == default_log_path
    ):
        return []

    if limit <= 0 or not GLM_FALLBACK_LOG_PATH.exists():
        return []

    normalized_repo = _normalize_scope_value(repo_root)
    collected: list[dict] = []

    try:
        with GLM_FALLBACK_LOG_PATH.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as e:
        print(f"[glm_classifier] fallback log read failed: {e}", file=sys.stderr)
        return []

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if normalized_repo and _normalize_scope_value(event.get("repo_root")) != normalized_repo:
            continue
        if reviewer and (event.get("reviewer") or "") != reviewer:
            continue

        collected.append(event)
        if len(collected) >= limit:
            break

    collected.reverse()
    return collected


def _should_suppress_glm(
    repo_root: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> bool:
    """直近 429 多発時は GLM API 呼び出しを一時抑制する。"""
    recent_events = _load_recent_fallback_events(
        repo_root=repo_root,
        reviewer=reviewer,
        limit=GLM_SUPPRESSION_LOOKBACK,
    )
    if not recent_events:
        return False

    recent_429 = sum(1 for event in recent_events if event.get("reason") == "http_429")
    return recent_429 >= GLM_HTTP_429_SUPPRESSION_THRESHOLD


def classify_finding(
    summary: str,
    severity: str = "warning",
    file_path: Optional[str] = None,
    reviewer: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """
    Finding summary を13カテゴリに分類する。

    Args:
        summary: finding のサマリーテキスト
        severity: finding の severity（分類のヒントとして使用）
        file_path: 対象ファイルパス（分類のヒントとして使用）
        reviewer: 呼び出し元 reviewer 名（観測ログ用）
        repo_root: リポジトリルート（観測ログ用）

    Returns:
        {"category": str, "confidence": float, "source": "glm"|"fallback", "fallback_reason": str|None}
    """
    token = _get_api_token()
    if not token:
        # API トークンなし → validate_category によるルールベースフォールバック
        fallback = _build_fallback_result(summary, "no_token")
        _append_fallback_event(summary, severity, file_path, "no_token", fallback["source"], reviewer, repo_root)
        return fallback

    if _should_suppress_glm(repo_root=repo_root, reviewer=reviewer):
        fallback = _build_fallback_result(summary, "suppressed_http_429")
        _append_fallback_event(
            summary,
            severity,
            file_path,
            "suppressed_http_429",
            fallback["source"],
            reviewer,
            repo_root,
        )
        return fallback

    # コンテキストを付加してプロンプト構成
    context_hint = ""
    if file_path:
        context_hint += f"\nファイル: {file_path}"
    if severity:
        context_hint += f"\nSeverity: {severity}"

    prompt_text = CLASSIFICATION_PROMPT.format(summary=summary + context_hint)

    last_failure_reason = "unknown"

    for attempt in range(MAX_RETRIES + 1):
        try:
            result = _call_glm_api(token, prompt_text)
            if result and result.get("category") in VALID_CATEGORIES:
                result["source"] = "glm"
                result["fallback_reason"] = None
                return result
            if result is None:
                last_failure_reason = "empty_response"
            else:
                last_failure_reason = "invalid_category"
            # カテゴリが不正 → リトライ
            if attempt < MAX_RETRIES:
                continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[glm_classifier] API error (attempt {attempt + 1}): {e}", file=sys.stderr)
            last_failure_reason = _describe_api_error(e)
            if attempt < MAX_RETRIES and _should_retry_api_error(e):
                continue
            break

    # 全リトライ失敗 → フォールバック
    fallback = _build_fallback_result(summary, last_failure_reason)
    _append_fallback_event(summary, severity, file_path, last_failure_reason, fallback["source"], reviewer, repo_root)
    return fallback


def _call_glm_api(token: str, prompt: str) -> Optional[dict]:
    """Z.ai の Anthropic 互換 API 経由で GLM を呼び出し、JSON レスポンスを返す。"""
    payload = json.dumps({
        "model": GLM_MODEL,
        "max_tokens": 100,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        GLM_API_URL,
        data=payload,
        headers={
            "x-api-key": token,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        },
    )

    resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    body = json.loads(resp.read().decode("utf-8"))

    content_blocks = body.get("content") or []
    text_parts = [
        block.get("text", "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    content = "\n".join(part for part in text_parts if part)
    if not content:
        return None
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
            f["classification_fallback_reason"] = "empty_summary"
            results.append(f)
            continue

        classification = classify_finding(
            summary=summary,
            severity=f.get("severity", "warning"),
            file_path=f.get("file_path"),
            reviewer=f.get("reviewer"),
            repo_root=f.get("repo_root"),
        )
        f["classified_category"] = classification["category"]
        f["classification_confidence"] = classification.get("confidence", 0.5)
        f["classification_source"] = classification.get("source", "unknown")
        f["classification_fallback_reason"] = classification.get("fallback_reason")
        results.append(f)

    return results
