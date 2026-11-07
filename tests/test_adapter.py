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
