import subprocess

import pytest

from whyagent.replay import DockerSandbox, Replayer, docker_available
from whyagent.rerun import Agent, Reruns, decide, make_lesson, task_prompt
from whyagent.rerun.lesson import build_prompt
from whyagent.tiger import RunLog
from whyagent.types import RootCause, State, Step

docker_ok, docker_why = docker_available()


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", check=True).stdout.strip()


def _step(i, old, new, **kw):
    return Step(i, "sess", "claude-code", kw.pop("file", "app.py"), "edit", old, new, prompt="Fix the cert error", **kw)


def _rc(base):
    edits = [_step(1, "# config", "# config v2", reasoning="Tidy the header.")]
    guilty = _step(2, "VERIFY = True", "VERIFY = False", reasoning="Disabling verification to unblock.",
                   trigger="SSLError: certificate verify failed")
    return RootCause(step=guilty, collateral=[], before_state=State(base, [1], base), edits_before=edits,
                     failing_output="AssertionError", head_state=State(base, [1, 2], base))


def test_task_prompt_and_lesson():
    rc = _rc("abc")
    p = task_prompt(rc, None)
    assert p.startswith("Fix the cert error") and "app.py: Tidy the header." in p and "Lesson" not in p
    assert task_prompt(rc, "Never disable TLS.").endswith("(follow it): Never disable TLS.")
    seen = []
    lesson = make_lesson(rc, generate=lambda prompt: seen.append(prompt) or
                         'Lesson: "Never disable SSL verification; mount the CA bundle instead." Extra words.')
    assert lesson == "Never disable SSL verification; mount the CA bundle instead."
    assert "certificate verify failed" in seen[0] and "VERIFY = False" in seen[0]
    hidden = Step(3, "s", "a", "f.py", "edit", "a", "b", reasoning=None)
    assert "reasoning was hidden" in build_prompt(hidden)


def test_root_cause_roundtrip():
    from dataclasses import asdict
    rc = _rc("abc")
    assert RootCause.from_dict(asdict(rc)) == rc


def test_runlog_noop_without_url():
    log = RunLog(url="")
    assert not log.enabled
    log.log("bisect", "k", "PASS", 10)  # no-op, no error


@pytest.mark.skipif(not docker_ok, reason=docker_why)
def test_reruns_in_docker(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("# config\nVERIFY = True\nOTHER = 1\n")
    (tmp_path / "conftest.py").write_text("# pinned\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/check.py").write_text("import sys; sys.path.insert(0, '.'); import app; sys.exit(0 if app.VERIFY else 1)\n")
    (tmp_path / "tests/suite.py").write_text(
        "import sys; sys.path.insert(0, '.'); import app\n"
        "if app.VERIFY: print('PASSED tests/check.py::verify')\n"
        "if app.OTHER == 1: print('PASSED tests/suite.py::other')\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    rc = _rc(base)
    sb = DockerSandbox(str(tmp_path), base, setup="true", quiet=True)
    rp = Replayer(str(tmp_path), rc.edits_before + [rc.step], "python tests/check.py", sandbox=sb)

    # Repeats the mistake without the lesson, avoids it with the lesson.
    agent = Agent("fake", 'case "$WHYAGENT_PROMPT" in *Lesson*) echo ok ;; '
                          '*) sed -i "s/VERIFY = True/VERIFY = False/" app.py ;; esac', network="none")
    a, b = Reruns(rp, agent, suite_cmd="python tests/suite.py", workers=2).run(rc, "Never disable TLS.", 2)
    assert [r.outcome for r in a] == ["FAIL", "FAIL"] and [r.outcome for r in b] == ["PASS", "PASS"]
    assert decide(0, 2, 2, 2)[0] in ("PROMISING", "VALIDATED")

    def single(cmd):
        return Reruns(rp, Agent("fake", cmd, network="none"), suite_cmd="python tests/suite.py").one(rc, None, "A")

    assert single("echo '# hacked' > conftest.py").outcome == "TAMPERED"
    assert single("sed -i 's/OTHER = 1/OTHER = 2/' app.py").outcome == "REGRESSED"
    # tests/ is mounted read-only: the write fails and the test file is unchanged
    assert single("echo 'import sys; sys.exit(0)' > tests/check.py; sed -i 's/VERIFY = True/VERIFY = False/' app.py").outcome == "FAIL"


def test_gemini_error_detection():
    from whyagent.rerun import GeminiCLI
    g = GeminiCLI()
    quota = 'TerminalQuotaError: blah\n{\n  "session_id": "x",\n  "error": {"type": "Error", "message": "You have exhausted your daily quota on this model.", "code": 429}\n}\n'
    assert "429" in g.error(quota, 1)
    ok = '{"response": "done", "stats": {"models": {"m": {"tokens": {"total": 1234}}}}}'
    assert g.error(ok, 0) is None and g.tokens(ok) == 1234
    turns = '{"error": {"message": "Reached max session turns for this session."}}'
    assert g.error(turns, 1) is None


def test_claude_code_agent(monkeypatch):
    from whyagent.rerun import ClaudeCode, agent_by_name
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    c = agent_by_name("claude-code")
    assert isinstance(c, ClaudeCode) and "--max-turns 15" in c.command
    ok = '{"type":"result","subtype":"success","is_error":false,"num_turns":6,"usage":{"input_tokens":100,"output_tokens":20,"cache_read_input_tokens":5}}'
    assert c.error(ok, 0) is None and c.tokens(ok) == 125
    auth = '{"type":"result","subtype":"success","is_error":true,"num_turns":1,"result":"Invalid API key"}'
    assert "Invalid API key" in c.error(auth, 1)
    capped = '{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":15}'
    assert c.error(capped, 1) is None


def _online():
    import socket
    try:
        socket.create_connection(("example.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not docker_ok, reason=docker_why)
@pytest.mark.skipif(not _online(), reason="needs internet")
def test_egress_allowlist(tmp_path):
    from whyagent.replay.egress import Egress
    base = _repo_simple(tmp_path)
    sb = DockerSandbox(str(tmp_path), base, setup="true", quiet=True)
    sb.prepare()
    fetch = ("python -c \"import sys, urllib.request as u\n"
             "try: u.urlopen('https://' + sys.argv[1], timeout=8); print('OPEN')\n"
             "except Exception as e: print('BLOCKED', type(e).__name__)\" ")
    with Egress(sb.image, ["example.com"]) as eg:
        run = lambda host, env: sb.exec(str(tmp_path), fetch + host, 30, env=env, network=eg.network).output  # noqa: E731
        assert "OPEN" in run("example.com", eg.env)
        assert "BLOCKED" in run("www.google.com", eg.env)
        assert "BLOCKED" in run("example.com", {"X": "1"})  # bypassing the proxy: internal network, no route out
    assert eg.blocked == ["www.google.com:443"]
    nets = subprocess.run(["docker", "network", "ls", "-q", "--filter", f"name={eg.network}"],
                          capture_output=True, encoding="utf-8").stdout.strip()
    assert nets == ""


def _repo_simple(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return git(tmp_path, "rev-parse", "HEAD")


def test_gemini_retries_5xx_only(monkeypatch):
    import types as T

    from whyagent.rerun import lesson

    calls = []

    class Err(Exception):
        def __init__(self, code):
            self.code = code

    def fake_client(**_):
        def gen(model, contents):
            calls.append(model)
            if len(calls) < 3:
                raise Err(503)
            return T.SimpleNamespace(text="Ok.")
        return T.SimpleNamespace(models=T.SimpleNamespace(generate_content=gen))

    import google.genai as genai
    monkeypatch.setattr(genai, "Client", fake_client)
    monkeypatch.setattr(lesson.time, "sleep", lambda s: None)
    assert lesson.gemini_generate("p", model="m") == "Ok." and len(calls) == 3

    calls.clear()

    def quota(**_):
        def gen(model, contents):
            calls.append(model)
            raise Err(429)
        return T.SimpleNamespace(models=T.SimpleNamespace(generate_content=gen))

    monkeypatch.setattr(genai, "Client", quota)
    with pytest.raises(Err):
        lesson.gemini_generate("p", model="m")
    assert len(calls) == 1


def test_baseline_leaves_out_tests_the_session_added():
    """Tests that only pass because of code the original agent invented aren't required of reruns."""
    from types import SimpleNamespace

    outputs = {  # what `pytest -rA` prints in each state
        "G": "PASSED tests/t.py::old_behaviour\nPASSED tests/t.py::iter_all_invoices_new",
        "parent": "PASSED tests/t.py::old_behaviour\nFAILED tests/t.py::iter_all_invoices_new",
    }

    class FakeRP:
        timeout = 10
        sandbox = SimpleNamespace(exec=lambda d, cmd, timeout: SimpleNamespace(output=outputs[d]))

        def build(self, state):
            return "G" if state.step_ids else "parent"

        def teardown(self, d):
            pass

    rc = _rc("abc")
    rr = Reruns(FakeRP(), Agent("fake", "true", network="none"), suite_cmd="pytest -rA")
    rr.prepare(rc)
    assert rr._baseline == {"tests/t.py::old_behaviour"}
    assert rr.excluded == ["tests/t.py::iter_all_invoices_new"]
