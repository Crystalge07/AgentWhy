from whyagent.trail import finding_body, post_finding, revert_patch
from whyagent.types import RootCause, State, Step

HEAD = "TIMEOUT_S = 2\nMAX_RETRIES = 1\nPAGE_SIZE = 50\n"


def rc():
    s = Step(8, "sess", "claude-code", "payments/config.py", "shell", "TIMEOUT_S = 10\nMAX_RETRIES = 3\n",
             "TIMEOUT_S = 2\nMAX_RETRIES = 1\n", reasoning="Lowering MAX_RETRIES globally will break a test.",
             prompt="change the defaults in config.py", command="sed -i 's/3/1/' payments/config.py")
    st = State("abc", [], "abc")
    return RootCause(step=s, collateral=[], before_state=st, edits_before=[], failing_output="", head_state=st)


REPORT = {"meta": {"test": "python -m pytest -q tests/test_client.py::test_retries"}, "runs": [{}] * 6,
          "precheck": {"mode": "stable"}, "blame_hint": {"line": "payments/client.py:39", "is_cause": False},
          "lesson": {"text": "Add a per-instance option instead of changing shared defaults."},
          "rerun": {"agent": "claude-code", "a": ["FAIL"] * 5, "b": ["PASS"] * 5, "p": 0.004, "verdict": "VALIDATED"}}


def test_revert_patch_undoes_the_edit_at_head():
    diff, line = revert_patch(rc(), HEAD)
    assert line == 1 and "-MAX_RETRIES = 1" in diff and "+MAX_RETRIES = 3" in diff
    assert diff.startswith("--- a/payments/config.py\n+++ b/payments/config.py\n")
    assert revert_patch(rc(), "TIMEOUT_S = 5\n") is None  # edit no longer at HEAD: no patch


def test_finding_body_has_proof_reasoning_and_verdict():
    body = finding_body(rc(), REPORT)
    for want in ("`tests/test_client.py::test_retries`", "6 test runs", "payments/client.py:39", "sed -i",
                 "Lowering MAX_RETRIES", "per-instance option", "0/5 pass", "5/5 pass", "VALIDATED (p = 0.004)"):
        assert want in body, want


def test_post_finding_builds_the_entire_command(tmp_path, monkeypatch):
    import whyagent.trail as trail
    monkeypatch.setattr(trail, "_head_text", lambda path, cwd=".": HEAD)
    seen = {}

    def fake_run(args, cwd="."):
        seen["args"] = args
        seen["patch"] = open(args[args.index("--patch-file") + 1]).read()
        return "https://entire.io/finding/1"

    assert post_finding(rc(), REPORT, client_id="whyagent-x", run=fake_run) == "https://entire.io/finding/1"
    a = seen["args"]
    assert a[:3] == ["trail", "finding", "add"] and a[a.index("--file") + 1] == "payments/config.py"
    assert a[a.index("--line") + 1] == "1" and a[a.index("--severity") + 1] == "high"
    assert "+MAX_RETRIES = 3" in seen["patch"]
