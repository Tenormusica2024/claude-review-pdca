"""
glm_classifier.py のユニットテスト。

テスト対象:
- _fallback_classify: キーワードベース分類
- _extract_json: JSON 抽出
- classify_finding: API 呼び出し + フォールバック
- classify_findings_batch: バッチ分類
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from glm_classifier import (
    _fallback_classify,
    _extract_json,
    classify_finding,
    classify_findings_batch,
    VALID_CATEGORIES,
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
        result = classify_finding("SQL injection risk")
        assert result["source"] == "fallback"
        assert result["category"] == "security"

    @patch("glm_classifier._call_glm_api")
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_successful_api_call(self, mock_token, mock_api):
        mock_api.return_value = {"category": "logic", "confidence": 0.95}
        result = classify_finding("off-by-one in loop")
        assert result["category"] == "logic"
        assert result["confidence"] == 0.95
        assert result["source"] == "glm"

    @patch("glm_classifier._call_glm_api")
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_invalid_category_retries_then_fallback(self, mock_token, mock_api):
        """API が不正カテゴリを返した場合、リトライ後フォールバック。"""
        mock_api.return_value = {"category": "invalid-xyz", "confidence": 0.9}
        result = classify_finding("SQL injection")
        assert result["source"] == "fallback"

    @patch("glm_classifier._call_glm_api", side_effect=TimeoutError("timeout"))
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_api_timeout_falls_back(self, mock_token, mock_api):
        result = classify_finding("missing error handling")
        assert result["source"] == "fallback"
        assert result["category"] == "robustness"

    @patch("glm_classifier._call_glm_api")
    @patch("glm_classifier._get_api_token", return_value="test-token")
    def test_retry_succeeds_on_second_attempt(self, mock_token, mock_api):
        """1回目失敗、2回目成功。"""
        mock_api.side_effect = [
            {"category": "invalid", "confidence": 0.5},  # 1回目: 不正カテゴリ
            {"category": "security", "confidence": 0.85},  # 2回目: 正常
        ]
        result = classify_finding("auth token leak")
        assert result["category"] == "security"
        assert result["source"] == "glm"


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

    @patch("glm_classifier._get_api_token", return_value=None)
    def test_max_batch_limit(self, mock_token):
        findings = [{"summary": f"issue {i}", "severity": "warning"} for i in range(30)]
        results = classify_findings_batch(findings, max_batch=5)
        assert len(results) == 5

    @patch("glm_classifier._get_api_token", return_value=None)
    def test_empty_list(self, mock_token):
        results = classify_findings_batch([])
        assert results == []
