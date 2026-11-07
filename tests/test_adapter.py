from pathlib import Path

from whyagent.adapters import merge_sessions, parse
from whyagent.types import Step


def _s(sess, ts, new):
    return Step(0, sess, "claude-code", "a.py", "edit", "x", new, ts=ts)


def test_merge_sessions_interleaves_by_timestamp_and_renumbers():
    a = [_s("A", "2026-11-07T10:00:00Z", "a1"), _s("A", "2026-11-07T10:00:05Z", "a2")]
    b = [_s("B", "2026-11-07T10:00:02Z", "b1"), _s("B", "", "b2"), _s("B", "2026-11-07T10:00:01Z", "b3")]
    merged = merge_sessions([a, b])
    # b2 has no timestamp and b3's is skewed earlier; both stay after b1, in session order
    assert [s.new_text for s in merged] == ["a1", "b1", "b2", "b3", "a2"]
    assert [s.id for s in merged] == [1, 2, 3, 4, 5]


def test_parse_records_timestamps():
    line = ('{"type":"assistant","timestamp":"2026-11-07T10:00:00Z","message":{"content":[{"type":"tool_use",'
            '"id":"t1","name":"Edit","input":{"file_path":"a.py","old_string":"x","new_string":"y"}}]}}')
    assert parse(line)[0].ts == "2026-11-07T10:00:00Z"


def test_parse_claude_code_fixture():
    steps = parse((Path(__file__).parent / "fixtures/claude_session.jsonl").read_text(encoding="utf-8"))
    assert [s.file for s in steps] == ["config.py", "new.py"]
    s = steps[0]
    assert s.session_id == "abc123"
    assert s.new_text == "VERIFY = False"
    assert s.trigger == "SSLError: certificate verify failed"
    assert s.reasoning == "cert error, disabling verification to unblock"
    assert s.prompt.startswith("Fix the certificate")


def test_parse_real_transcript_shape():
    """Quirks seen in real Entire-recorded Claude Code sessions (verified 2026-11-07)."""
    steps = parse((Path(__file__).parent / "fixtures/claude_session_real_shape.jsonl").read_text(encoding="utf-8"))
    # the same tool_use id appears in two records; it must only be applied once
    assert len(steps) == 2
    # absolute paths become repo-relative (POSIX and Windows recordings), so replay works on any machine
    assert [s.file for s in steps] == ["src/config.py", "src/client.py"]
    s = steps[0]
    # injected <system-reminder> blocks (e.g. CLAUDE.md) are not part of the user's prompt
    assert s.prompt == "Fix the certificate error when calling the API"
    # empty (hidden) thinking blocks don't wipe out visible reasoning
    assert s.reasoning == "cert error, disabling verification to unblock"
    assert s.trigger == "SSLError: certificate verify failed"
    assert [s.id for s in steps] == [1, 2]


def test_trigger_is_command_output_not_file_reads():
    import json

    def rec(kind, content):
        return json.dumps({"type": kind, "message": {"content": content}})

    lines = [
        rec("user", "fix it"),
        rec("assistant", [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "pytest"}}]),
        rec("user", [{"type": "tool_result", "tool_use_id": "b1", "content": "E   SSLError: certificate verify failed"}]),
        rec("assistant", [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "a.py"}}]),
        rec("user", [{"type": "tool_result", "tool_use_id": "r1", "content": "1\tVERIFY = True"}]),
        rec("assistant", [{"type": "tool_use", "id": "e1", "name": "Edit",
                           "input": {"file_path": "a.py", "old_string": "True", "new_string": "False"}}]),
        rec("user", [{"type": "tool_result", "tool_use_id": "e1", "content": "updated successfully"}]),
        rec("assistant", [{"type": "tool_use", "id": "g1", "name": "Grep", "input": {}}]),
        rec("user", [{"type": "tool_result", "tool_use_id": "g1", "content": "boom", "is_error": True}]),
        rec("assistant", [{"type": "tool_use", "id": "e2", "name": "Edit",
                           "input": {"file_path": "a.py", "old_string": "False", "new_string": "None"}}]),
    ]
    s1, s2 = parse("\n".join(lines))
    assert s1.trigger == "E   SSLError: certificate verify failed"  # the Read in between doesn't replace it
    assert s2.trigger == "boom"                                     # but a tool error does


def test_trigger_line_picks_the_error():
    from whyagent.pipeline import trigger_line

    out = "....F\n=========== FAILURES ===========\nE   AssertionError: assert 20 == 25\n1 failed in 0.3s\n"
    assert trigger_line(out) == "E   AssertionError: assert 20 == 25"
    ssl = "requests.exceptions.SSLError: certificate verify failed"
    assert trigger_line(ssl + "\nexit 1") == ssl
    assert trigger_line("collected 3 items\nall good") == "all good"
    assert trigger_line(None) == ""


def test_view_only_commands_are_not_triggers():
    from whyagent.adapters.claude_code import _is_view_only

    assert _is_view_only("git ls-files && cat config.py 2>/dev/null; grep -rn invoice --include=*.py . | head -50")
    assert _is_view_only("cd /repo && cat scripts/smoke_dev.py README.md; sed -n 1,39p scripts/dev_server.py")
    assert _is_view_only("git log --oneline -3 && git status --short")
    assert not _is_view_only(".venv/Scripts/python -m pytest -q 2>&1 | tail -15")
    assert not _is_view_only("cd /repo && python scripts/smoke_dev.py")
    assert not _is_view_only("sed -i 's/a/b/' x.py")
    assert not _is_view_only("git commit -am wip")
