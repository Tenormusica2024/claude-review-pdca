"""
summarize-glm-fallbacks.py のテスト。
"""
import importlib.util
import json
from unittest.mock import patch


def _load_module():
    script_path = __file__
    from pathlib import Path

    target = Path(script_path).resolve().parent.parent / "scripts" / "summarize-glm-fallbacks.py"
    spec = importlib.util.spec_from_file_location("summarize_glm_fallbacks_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


summarize_glm_fallbacks = _load_module()
_build_recent_reviewer_summary = summarize_glm_fallbacks._build_recent_reviewer_summary
_filter_events = summarize_glm_fallbacks._filter_events
_load_events = summarize_glm_fallbacks._load_events


class TestSummarizeGlmFallbacks:
    def test_load_events_skips_invalid_json(self, tmp_path):
        log_path = tmp_path / "glm-fallbacks.jsonl"
        log_path.write_text(
            '{"reason":"http_429","reviewer":"r1"}\n'
            'not-json\n'
            '{"reason":"timeout","reviewer":"r2"}\n',
            encoding="utf-8",
        )

        events = _load_events(log_path)

        assert len(events) == 2
        assert [event["reason"] for event in events] == ["http_429", "timeout"]

    def test_load_events_returns_empty_on_os_error(self, tmp_path):
        log_path = tmp_path / "glm-fallbacks.jsonl"
        log_path.write_text('{"reason":"http_429","reviewer":"r1"}\n', encoding="utf-8")

        with patch("pathlib.Path.open", side_effect=OSError("sharing violation")):
            events = _load_events(log_path)

        assert events == []

    def test_filter_events_by_repo_and_reviewer(self):
        events = [
            {"reason": "http_429", "reviewer": "r1", "repo_root": "C:/repo-a"},
            {"reason": "timeout", "reviewer": "r1", "repo_root": "C:/repo-b"},
            {"reason": "no_token", "reviewer": "r2", "repo_root": "C:/repo-a"},
        ]

        filtered = _filter_events(events, repo_root="C:\\repo-a", reviewer="r1")

        assert filtered == [{"reason": "http_429", "reviewer": "r1", "repo_root": "C:/repo-a"}]

    def test_build_recent_reviewer_summary(self):
        events = [
            {"reason": "http_429", "reviewer": "r1"},
            {"reason": "suppressed_http_429", "reviewer": "r1"},
            {"reason": "timeout", "reviewer": "r2"},
            {"reason": "http_429", "reviewer": "r2"},
        ]

        recent = _build_recent_reviewer_summary(events, 3)

        assert recent == {
            "r1": {"suppressed_http_429": 1},
            "r2": {"timeout": 1, "http_429": 1},
        }

    def test_main_json_empty_output_has_stable_keys(self, capsys, tmp_path):
        log_path = tmp_path / "missing.jsonl"

        with patch("sys.argv", ["summarize-glm-fallbacks.py", "--json", "--log-path", str(log_path)]):
            summarize_glm_fallbacks.main()

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "total": 0,
            "by_reason": {},
            "by_severity": {},
            "by_reviewer": {},
            "by_repo_root": {},
            "recent_by_reviewer": {},
            "recent": [],
        }
