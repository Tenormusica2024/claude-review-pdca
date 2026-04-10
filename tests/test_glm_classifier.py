"""
glm_classifier.py のユニットテスト。

テスト対象:
- _fallback_classify: キーワードベース分類
- _extract_json: JSON 抽出
- classify_finding: API 呼び出し + フォールバック
- classify_findings_batch: バッチ分類
"""
import json
import urllib.error
import pytest
from unittest.mock import patch, MagicMock

from glm_classifier import (
    _call_glm_api,
    _append_fallback_event,
    _describe_api_error,
    _fallback_classify,
    _extract_json,
    _load_recent_fallback_events,
    _should_retry_api_error,
    _should_suppress_glm,
    classify_finding,
    classify_findings_batch,
    VALID_CATEGORIES,
    GLM_API_URL,
    GLM_MODEL,
    ANTHROPIC_VERSION,
)


class TestFallbackClassify:
    """キーワードベース分類のテスト。"""

    def test_security_keywords(self):
        result = _fallback_classify("SQL injection via string format")
        assert result["category"] == "security"
        assert result["source"] == "fallback"

    def test_logic_keywords(self):
        result = _fallback_classify("off-by-one error in loop boundary")
        assert result["category"] == "logic"

    def test_robustness_keywords(self):
        result = _fallback_classify("missing error handling for file read")
        assert result["category"] == "robustness"

    def test_concurrency_keywords(self):
        result = _fallback_classify("race condition in shared state update")
        assert result["category"] == "concurrency"

    def test_performance_keywords(self):
        result = _fallback_classify("slow query with O(N² complexity")
        assert result["category"] == "performance"

    def test_test_quality_keywords(self):
        result = _fallback_classify("test coverage missing for edge case")
        assert result["category"] == "test-quality"

    def test_unknown_falls_to_maintainability(self):
        result = _fallback_classify("some completely unrelated text xyz")
        assert result["category"] == "maintainability"
        assert result["confidence"] == 0.3

    def test_all_results_have_valid_category(self):
        """全結果が13カテゴリ enum 内であること。"""
        test_inputs = [
            "SQL injection", "off-by-one", "missing try-except",
            "deadlock risk", "null check missing", "slow O(n²)",
            "API contract violation", "test mock issue", "data integrity",
            "naming inconsistency", "missing docstring", "a11y issue",
            "random text here",
        ]
        for text in test_inputs:
            result = _fallback_classify(text)
            assert result["category"] in VALID_CATEGORIES, f"Invalid category for '{text}': {result['category']}"


class TestExtractJson:
    """JSON 抽出のテスト。"""

    def test_clean_json(self):
        result = _extract_json('{"category": "logic", "confidence": 0.9}')
        assert result["category"] == "logic"

    def test_json_with_surrounding_text(self):
        result = _extract_json('Here is the result: {"category": "security", "confidence": 0.8} done.')
        assert result["category"] == "security"

    def test_invalid_json_returns_none(self):
        result = _extract_json("not json at all")
        assert result is None

    def test_empty_string(self):
        result = _extract_json("")
        assert result is None

    def test_json_in_markdown_code_block(self):
        text = '```json\n{"category": "robustness", "confidence": 0.7}\n```'
        result = _extract_json(text)
        assert result["category"] == "robustness"


class TestClassifyFinding:
    """classify_finding のテスト（API モック）。"""

    @patch("glm_classifier._get_api_token", return_value=None)
    def test_no_token_uses_fallback(self, mock_token):
        with patch("glm_classifier._append_fallback_event") as mock_log:
            result = classify_finding("SQL injection risk")
        assert result["source"] == "fallback"
        assert result["category"] == "security"
        assert result["fallback_reason"] == "no_token"
        mock_log.assert_called_once()

    @patch("glm_classifier._call_glm_api")
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_successful_api_call(self, mock_token, mock_api):
        mock_api.return_value = {"category": "logic", "confidence": 0.95}
        with patch("glm_classifier._should_suppress_glm", return_value=False):
            result = classify_finding("off-by-one in loop")
        assert result["category"] == "logic"
        assert result["confidence"] == 0.95
        assert result["source"] == "glm"
        assert result["fallback_reason"] is None

    @patch("glm_classifier._call_glm_api")
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_invalid_category_retries_then_fallback(self, mock_token, mock_api):
        """API が不正カテゴリを返した場合、リトライ後フォールバック。"""
        mock_api.return_value = {"category": "invalid-xyz", "confidence": 0.9}
        with patch("glm_classifier._should_suppress_glm", return_value=False):
            result = classify_finding("SQL injection")
        assert result["source"] == "fallback"
        assert result["fallback_reason"] == "invalid_category"

    @patch("glm_classifier._call_glm_api", side_effect=TimeoutError("timeout"))
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_api_timeout_falls_back(self, mock_token, mock_api):
        with patch("glm_classifier._append_fallback_event") as mock_log:
            with patch("glm_classifier._should_suppress_glm", return_value=False):
                result = classify_finding("missing error handling")
        assert result["source"] == "fallback"
        assert result["category"] == "robustness"
        assert result["fallback_reason"] == "timeout"
        mock_log.assert_called_once()

    @patch("glm_classifier._call_glm_api")
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_retry_succeeds_on_second_attempt(self, mock_token, mock_api):
        """1回目失敗、2回目成功。"""
        mock_api.side_effect = [
            {"category": "invalid", "confidence": 0.5},  # 1回目: 不正カテゴリ
            {"category": "security", "confidence": 0.85},  # 2回目: 正常
        ]
        with patch("glm_classifier._should_suppress_glm", return_value=False):
            result = classify_finding("auth token leak")
        assert result["category"] == "security"
        assert result["source"] == "glm"

    @patch("glm_classifier._call_glm_api", side_effect=urllib.error.HTTPError(
        url=GLM_API_URL,
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=None,
    ))
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_rate_limit_does_not_retry(self, mock_token, mock_api):
        with patch("glm_classifier._append_fallback_event") as mock_log:
            with patch("glm_classifier._should_suppress_glm", return_value=False):
                result = classify_finding("auth token leak")
        assert result["source"] == "fallback"
        assert result["fallback_reason"] == "http_429"
        assert mock_api.call_count == 1
        mock_log.assert_called_once()

    @patch("glm_classifier._call_glm_api")
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_recent_429_suppresses_glm_call(self, mock_token, mock_api):
        with patch("glm_classifier._should_suppress_glm", return_value=True):
            with patch("glm_classifier._append_fallback_event") as mock_log:
                result = classify_finding(
                    "auth token leak",
                    reviewer="review-fix-loop",
                    repo_root="C:/repo",
                )
        assert result["source"] == "fallback"
        assert result["fallback_reason"] == "suppressed_http_429"
        mock_api.assert_not_called()
        mock_log.assert_called_once()


class TestShouldRetryApiError:
    """再試行可否判定のテスト。"""

    def test_http_429_is_not_retryable(self):
        error = urllib.error.HTTPError(GLM_API_URL, 429, "Too Many Requests", None, None)
        assert _should_retry_api_error(error) is False

    def test_http_401_is_not_retryable(self):
        error = urllib.error.HTTPError(GLM_API_URL, 401, "Unauthorized", None, None)
        assert _should_retry_api_error(error) is False

    def test_timeout_is_retryable(self):
        assert _should_retry_api_error(TimeoutError("timeout")) is True


class TestDescribeApiError:
    """観測用エラー種別の正規化テスト。"""

    def test_http_error_maps_to_status(self):
        error = urllib.error.HTTPError(GLM_API_URL, 403, "Forbidden", None, None)
        assert _describe_api_error(error) == "http_403"

    def test_timeout_maps_to_timeout(self):
        assert _describe_api_error(TimeoutError("timeout")) == "timeout"


class TestFallbackLogging:
    """fallback イベントの append-only ログ記録。"""

    def test_append_fallback_event_writes_jsonl(self, tmp_path):
        log_path = tmp_path / "glm-fallbacks.jsonl"
        with patch.dict("os.environ", {"GLM_CLASSIFIER_ENABLE_TEST_LOGGING": "1"}, clear=False):
            with patch("glm_classifier.GLM_FALLBACK_LOG_PATH", log_path):
                _append_fallback_event(
                    summary="auth token leak in debug output",
                    severity="warning",
                    file_path="src\\app.py",
                    reason="no_token",
                    source="fallback",
                    reviewer="review-fix-loop",
                    repo_root="C:\\Users\\Tenormusica\\claude-review-pdca",
                )

        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["reason"] == "no_token"
        assert event["source"] == "fallback"
        assert event["severity"] == "warning"
        assert event["reviewer"] == "review-fix-loop"
        assert event["repo_root"] == "C:/Users/Tenormusica/claude-review-pdca"
        assert event["file_path"] == "src/app.py"
        assert event["summary_preview"] == "auth token leak in debug output"
        assert len(event["summary_hash"]) == 12

    def test_append_fallback_event_is_disabled_under_pytest_by_default(self, tmp_path):
        log_path = tmp_path / "glm-fallbacks.jsonl"
        with patch("glm_classifier.GLM_FALLBACK_LOG_PATH", log_path):
            _append_fallback_event(
                summary="auth token leak in debug output",
                severity="warning",
                file_path="src\\app.py",
                reason="no_token",
                source="fallback",
                reviewer="review-fix-loop",
                repo_root="C:\\Users\\Tenormusica\\claude-review-pdca",
            )

        assert not log_path.exists()

    def test_load_recent_fallback_events_filters_by_scope(self, tmp_path):
        log_path = tmp_path / "glm-fallbacks.jsonl"
        rows = [
            {"reason": "http_429", "reviewer": "r1", "repo_root": "C:/repo-a"},
            {"reason": "timeout", "reviewer": "r1", "repo_root": "C:/repo-a"},
            {"reason": "http_429", "reviewer": "r2", "repo_root": "C:/repo-a"},
            {"reason": "http_429", "reviewer": "r1", "repo_root": "C:/repo-b"},
        ]
        log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

        with patch("glm_classifier.GLM_FALLBACK_LOG_PATH", log_path):
            events = _load_recent_fallback_events(repo_root="C:\\repo-a", reviewer="r1", limit=5)

        assert [event["reason"] for event in events] == ["http_429", "timeout"]

    def test_should_suppress_glm_when_recent_429_crosses_threshold(self, tmp_path):
        log_path = tmp_path / "glm-fallbacks.jsonl"
        rows = [
            {"reason": "http_429", "reviewer": "r1", "repo_root": "C:/repo-a"},
            {"reason": "timeout", "reviewer": "r1", "repo_root": "C:/repo-a"},
            {"reason": "http_429", "reviewer": "r1", "repo_root": "C:/repo-a"},
            {"reason": "http_429", "reviewer": "r1", "repo_root": "C:/repo-a"},
        ]
        log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

        with patch("glm_classifier.GLM_FALLBACK_LOG_PATH", log_path):
            assert _should_suppress_glm(repo_root="C:/repo-a", reviewer="r1") is True

    def test_should_not_suppress_glm_for_other_scope(self, tmp_path):
        log_path = tmp_path / "glm-fallbacks.jsonl"
        rows = [
            {"reason": "http_429", "reviewer": "r2", "repo_root": "C:/repo-a"},
            {"reason": "http_429", "reviewer": "r2", "repo_root": "C:/repo-a"},
            {"reason": "http_429", "reviewer": "r2", "repo_root": "C:/repo-a"},
        ]
        log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

        with patch("glm_classifier.GLM_FALLBACK_LOG_PATH", log_path):
            assert _should_suppress_glm(repo_root="C:/repo-a", reviewer="r1") is False


class TestCallGlmApi:
    """_call_glm_api のリクエスト/レスポンス整合性テスト。"""

    @patch("glm_classifier.urllib.request.urlopen")
    def test_anthropic_compatible_request_and_response(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "content": [
                {"type": "text", "text": '{"category": "logic", "confidence": 0.91}'}
            ]
        }).encode("utf-8")
        mock_urlopen.return_value = mock_response

        result = _call_glm_api("test-token", "classify this")

        assert result == {"category": "logic", "confidence": 0.91}
        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        assert request.full_url == GLM_API_URL
        assert request.headers["X-api-key"] == "test-token"
        assert request.headers["Anthropic-version"] == ANTHROPIC_VERSION
        assert payload["model"] == GLM_MODEL
        assert payload["messages"] == [{"role": "user", "content": "classify this"}]
        assert "response_format" not in payload

    @patch("glm_classifier.urllib.request.urlopen")
    def test_missing_text_content_returns_none(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "content": [{"type": "tool_use", "name": "noop"}]
        }).encode("utf-8")
        mock_urlopen.return_value = mock_response

        assert _call_glm_api("test-token", "classify this") is None


class TestClassifyFindingsBatch:
    """バッチ分類のテスト。"""

    @patch("glm_classifier._get_api_token", return_value=None)
    def test_batch_with_fallback(self, mock_token):
        findings = [
            {"summary": "SQL injection", "severity": "critical"},
            {"summary": "off-by-one error", "severity": "warning"},
            {"summary": "", "severity": "info"},  # 空 summary
        ]
        results = classify_findings_batch(findings)
        assert len(results) == 3
        assert results[0]["classified_category"] == "security"
        assert results[1]["classified_category"] == "logic"
        assert results[2]["classified_category"] == "maintainability"
        assert results[2]["classification_source"] == "skip"
        assert results[2]["classification_fallback_reason"] == "empty_summary"

    @patch("glm_classifier._get_api_token", return_value=None)
    def test_max_batch_limit(self, mock_token):
        findings = [{"summary": f"issue {i}", "severity": "warning"} for i in range(30)]
        results = classify_findings_batch(findings, max_batch=5)
        assert len(results) == 5

    @patch("glm_classifier._get_api_token", return_value=None)
    def test_empty_list(self, mock_token):
        results = classify_findings_batch([])
        assert results == []

    @patch("glm_classifier._get_api_token", return_value="test-token")
    @patch("glm_classifier._call_glm_api", side_effect=urllib.error.HTTPError(
        url=GLM_API_URL,
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=None,
    ))
    def test_batch_exposes_fallback_reason(self, mock_api, mock_token):
        findings = [{"summary": "auth token leak", "severity": "warning"}]
        results = classify_findings_batch(findings)
        assert results[0]["classification_source"] == "fallback"
        assert results[0]["classification_fallback_reason"] == "http_429"
