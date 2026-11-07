from pathlib import Path

from whyagent.adapters import parse


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
