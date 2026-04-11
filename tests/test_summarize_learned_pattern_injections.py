"""
summarize-learned-pattern-injections.py のテスト。
"""
import importlib.util
import json
from unittest.mock import patch


def _load_module():
    from pathlib import Path

    target = Path(__file__).resolve().parent.parent / "scripts" / "summarize-learned-pattern-injections.py"
    spec = importlib.util.spec_from_file_location("summarize_learned_pattern_injections_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


summarize_learned_pattern_injections = _load_module()
_load_events = summarize_learned_pattern_injections._load_events
_filter_events = summarize_learned_pattern_injections._filter_events


class TestSummarizeLearnedPatternInjections:
    def test_load_events_skips_invalid_json(self, tmp_path):
        log_path = tmp_path / "learned-pattern-injections.jsonl"
        log_path.write_text(
            '{"tool_name":"Edit","file_path":"src/a.py"}\n'
            'not-json\n'
            '{"tool_name":"Write","file_path":"src/b.py"}\n',
            encoding="utf-8",
        )

        events = _load_events(log_path)

        assert len(events) == 2
        assert [event["tool_name"] for event in events] == ["Edit", "Write"]

    def test_filter_events_by_repo_and_file(self):
        events = [
            {"repo_root": "C:/repo-a", "file_path": "src/a.py", "reviewer": "sc-rfl"},
            {"repo_root": "C:/repo-a", "file_path": "src/b.py", "reviewer": "sc-rfl"},
            {"repo_root": "C:/repo-b", "file_path": "src/a.py", "reviewer": "sc-ir"},
        ]

        filtered = _filter_events(events, repo_root="C:\\repo-a", file_path="src/a.py", reviewer="sc-rfl")

        assert filtered == [{"repo_root": "C:/repo-a", "file_path": "src/a.py", "reviewer": "sc-rfl"}]

    def test_main_json_empty_output_has_stable_keys(self, capsys, tmp_path):
        log_path = tmp_path / "missing.jsonl"

        with patch(
            "sys.argv",
            ["summarize-learned-pattern-injections.py", "--json", "--log-path", str(log_path)],
        ):
            summarize_learned_pattern_injections.main()

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "total": 0,
            "by_repo_root": {},
            "by_file_path": {},
            "by_tool_name": {},
            "by_reviewer": {},
            "by_category": {},
            "recent": [],
        }

    def test_main_json_summary_counts_by_dimensions(self, capsys, tmp_path):
        log_path = tmp_path / "learned-pattern-injections.jsonl"
        rows = [
            {
                "repo_root": "C:/repo-a",
                "file_path": "src/a.py",
                "tool_name": "Edit",
                "reviewer": "sc-rfl",
                "categories": ["logic", "security"],
                "pattern_count": 2,
            },
            {
                "repo_root": "C:/repo-a",
                "file_path": "src/a.py",
                "tool_name": "Write",
                "reviewer": "sc-rfl",
                "categories": ["logic"],
                "pattern_count": 1,
            },
        ]
        log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

        with patch(
            "sys.argv",
            ["summarize-learned-pattern-injections.py", "--json", "--log-path", str(log_path)],
        ):
            summarize_learned_pattern_injections.main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 2
        assert payload["by_repo_root"] == {"C:/repo-a": 2}
        assert payload["by_file_path"] == {"src/a.py": 2}
        assert payload["by_tool_name"] == {"Edit": 1, "Write": 1}
        assert payload["by_reviewer"] == {"sc-rfl": 2}
        assert payload["by_category"] == {"logic": 2, "security": 1}
