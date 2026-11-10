"""Sessions that look different from the demos: shell edits, multi-commit sessions, subdirectory cwds,
library frames in tracebacks, and failure signatures across code changes."""
import json

from whyagent.adapters import fix_paths, parse
from whyagent.adapters.claude_code import _is_view_only
from whyagent.adapters.fidelity import Fidelity, attribute_shell_edits, place_shell_edits, reconcile
from whyagent.replay import is_test_file, signature
from whyagent.rerun import decide
from whyagent.types import Step


def _pytest_out(body: str, test="tests/test_a.py::test_x", msg="RuntimeError: boom 42", secs="0.31") -> str:
    return (f"    def charge():\n{body}>       raise RuntimeError('boom')\nE       {msg}\n\n"
            f"payments/client.py:12: RuntimeError\n=== short test summary info ===\n"
            f"FAILED {test} - {msg}\n1 failed in {secs}s\n")


def test_signature_ignores_source_lines_and_timings_around_the_same_error():
    a = _pytest_out("        x = 1\n")
    b = _pytest_out("        retries = 3\n        x = retries - 2\n", secs="9.02", msg="RuntimeError: boom 7")
    assert signature(a) == signature(b)
    assert signature(a) != signature(_pytest_out("        x = 1\n", msg="ValueError: bad amount"))
    assert signature(a) != signature(_pytest_out("        x = 1\n", test="tests/test_a.py::test_y"))
    assert signature("make: *** [all] Error 2\n") == signature("make: *** [all] Error 2\n")  # no exception: tail hash


def test_redirects_into_files_are_edits_not_views():
    assert not _is_view_only('echo "TIMEOUT_S = 2" >> payments/config.py')
    assert not _is_view_only("printf 'x' > out.txt")
    assert _is_view_only("cat a.py 2>/dev/null")
    assert _is_view_only("grep -n x . 2>&1 | head")
    assert _is_view_only("echo done")


def _rec(kind, content, ts="2026-11-07T12:00:00.000Z", cwd="/repo"):
    return json.dumps({"type": kind, "sessionId": "s1", "cwd": cwd, "timestamp": ts, "message": {"content": content}})


def test_echo_redirect_edit_gets_the_agents_reasoning():
    lines = [_rec("user", "Make the client faster."),
             _rec("assistant", [{"type": "text", "text": "A 2s timeout is plenty."},
                                {"type": "tool_use", "id": "b1", "name": "Bash",
                                 "input": {"command": 'echo "TIMEOUT_S = 2" >> payments/config.py'}}])]
    shell = []
    parse("\n".join(lines), shell_out=shell)
    base, commit = {"payments/config.py": "X = 1\n"}, {"payments/config.py": "X = 1\nTIMEOUT_S = 2\n"}
    steps, fid = reconcile([], base.get, commit.get, ["payments/config.py"], lambda f: False)
    attribute_shell_edits(steps, fid, shell)
    assert steps[0].kind == "shell" and steps[0].reasoning == "A 2s timeout is plenty."
    assert steps[0].prompt == "Make the client faster."


def test_shell_attribution_prefers_commands_after_the_parent_commit():
    base, commit = {"a.py": "v = 1\n"}, {"a.py": "v = 2\n"}
    cmd = lambda ts, c, early: {"session_id": "s", "ts": ts, "command": c, "cwd": "/r", "reasoning": c,
                                "trigger": None, "prompt": "p", "early": early}
    # an earlier commit's command named a.py too; neither shows the new content
    shell = [cmd("2026-11-07T10:00:00Z", "python fix.py a.py", False), cmd("2026-11-07T09:00:00Z", "sed -i x a.py", True)]
    steps, fid = reconcile([], base.get, commit.get, ["a.py"], lambda f: False)
    attribute_shell_edits(steps, fid, shell)
    assert steps[0].command == "python fix.py a.py" and fid.shell_guessed == []
    # ...but a command that shows the content wins even if it came before the parent commit
    shell = [cmd("2026-11-07T10:00:00Z", "python fix.py a.py", False),
             cmd("2026-11-07T09:00:00Z", "sed -i 's/v = 1/v = 2/' a.py", True)]
    steps, fid = reconcile([], base.get, commit.get, ["a.py"], lambda f: False)
    attribute_shell_edits(steps, fid, shell)
    assert steps[0].command.startswith("sed")


def test_paths_from_a_subdirectory_cwd_map_onto_repo_files():
    tree = {"pkg/client.py", "pkg/config.py", "a/util.py", "b/util.py", "README.md"}
    steps = [Step(i, "s", "claude-code", f, "edit", "x", "y") for i, f in
             enumerate(["client.py", "/home/u/repo/pkg/config.py", "util.py", "README.md", "new.py"], 1)]
    fix_paths(steps, tree)
    assert [s.file for s in steps] == ["pkg/client.py", "pkg/config.py", "util.py", "README.md", "new.py"]


def test_shell_edits_are_placed_when_their_command_ran():
    base = {"a.py": "x = 1\n", "b.py": "y = 1\n"}
    commit = {"a.py": "x = 2\n", "b.py": "y = 2\n"}
    tool = Step(1, "s", "claude-code", "a.py", "edit", "x = 1\n", "x = 2\n", ts="2026-11-07T10:10:00Z")
    shell = [{"session_id": "s", "ts": "2026-11-07T10:05:00Z", "command": "sed -i s/1/2/ b.py", "cwd": "/r",
              "reasoning": "bump y", "trigger": None, "prompt": "p"}]
    steps, fid = reconcile([tool], base.get, commit.get, ["a.py", "b.py"], lambda f: False)
    attribute_shell_edits(steps, fid, shell)
    assert [s.file for s in steps] == ["a.py", "b.py"]  # appended after the tool edits
    steps = place_shell_edits(steps, fid, base.get)
    assert [(s.id, s.file, s.kind) for s in steps] == [(1, "b.py", "shell"), (2, "a.py", "edit")]
    assert fid.shell == [1]


def test_shell_edit_stays_at_the_end_when_moving_it_would_break_the_replay():
    base, commit = {"a.py": "v = 1\n"}, {"a.py": "v = 3\n"}
    tool = Step(1, "s", "claude-code", "a.py", "edit", "v = 1\n", "v = 2\n", ts="2026-11-07T10:10:00Z")
    shell = [{"session_id": "s", "ts": "2026-11-07T10:05:00Z", "command": "sed -i s/2/3/ a.py", "cwd": "/r",
              "reasoning": None, "trigger": None, "prompt": "p"}]
    steps, fid = reconcile([tool], base.get, commit.get, ["a.py"], lambda f: False)
    attribute_shell_edits(steps, fid, shell)
    steps = place_shell_edits(steps, fid, base.get)
    assert [s.kind for s in steps] == ["edit", "shell"]  # it edits the tool edit's output: order kept


def test_test_file_globs_cover_common_layouts():
    for f in ["tests/test_a.py", "pkg/tests/helpers.py", "pkg/client_test.py", "conftest.py", "src/test/x.py"]:
        assert is_test_file(f), f
    for f in ["pkg/client.py", "pkg/latest.py", "contest.py"]:
        assert not is_test_file(f), f


def test_traceback_hint_skips_library_frames():
    from whyagent.cli import line_from_traceback

    out = ("payments/client.py:40: in charge\n    requests.post(url)\n"
           "/usr/lib/python3.12/site-packages/requests/api.py:115: ConnectionError\n")
    assert line_from_traceback(out, exists=lambda p: not p.startswith("/")) == "payments/client.py:40"


def test_decide_respects_a_lower_alpha():
    # 0/5 vs 4/5: p ≈ 0.024, enough for one lesson but not for one of three
    assert decide(0, 5, 4, 5)[0] == "VALIDATED"
    assert decide(0, 5, 4, 5, alpha=0.05 / 3)[0] == "PROMISING"


def test_fidelity_tracks_stale_edits_separately():
    assert Fidelity(matches=True).stale == []
