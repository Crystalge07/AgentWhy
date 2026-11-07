from pathlib import Path

from whyagent.adapters import parse


def test_parse_claude_code_fixture():
    steps = parse((Path(__file__).parent / "fixtures/claude_session.jsonl").read_text())
    assert [s.file for s in steps] == ["config.py", "new.py"]
    s = steps[0]
    assert s.session_id == "abc123"
    assert s.new_text == "VERIFY = False"
    assert s.trigger == "SSLError: certificate verify failed"
    assert s.reasoning == "cert error, disabling verification to unblock"
    assert s.prompt.startswith("Fix the certificate")
