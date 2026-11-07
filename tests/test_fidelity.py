import random

from whyagent.adapters.fidelity import apply, hunks, occurrences, reconcile, split_writes
from whyagent.types import Step


def mk(i, f, kind, old, new):
    return Step(i, "s", "claude-code", f, kind, old, new)


def replay(text, pairs):
    for o, n in pairs:
        if o == "":
            assert not text
            text = n
        else:
            assert occurrences(text, o) == 1, f"hunk not unique when applied: {o!r}"
            text = text.replace(o, n, 1)
    return text


def test_hunks_are_unique_with_repeated_lines():
    old = "x = 1\nreturn x\n\nx = 1\nreturn x\n"
    new = "x = 1\nreturn x\n\nx = 2\nreturn x\n"
    hs = hunks(old, new)
    assert len(hs) == 1 and old.count(hs[0][0]) == 1
    assert replay(old, hs) == new


def test_hunks_property_random_files():
    rng = random.Random(7)
    vocab = ["a\n", "b\n", "c\n", "pass\n", "\n", "return x\n"]
    for _ in range(3000):
        old = "".join(rng.choice(vocab) for _ in range(rng.randint(0, 12)))
        new = "".join(rng.choice(vocab) for _ in range(rng.randint(0, 12)))
        assert (replay(old or None, hunks(old, new)) or "") == new, (old, new)


def test_split_writes_hunks_against_real_base():
    base = {"app.py": "import os\n\nDEBUG = False\nPORT = 80\n"}
    steps = [mk(1, "app.py", "write", "", "import os\n\nDEBUG = True\nPORT = 80\n"),
             mk(2, "app.py", "edit", "PORT = 80", "PORT = 8080"),
             mk(3, "new.py", "write", "", "x = 1\n"),
             mk(4, "app.py", "edit_all", "os", "sys")]
    out = split_writes(steps, base.get)
    assert [s.kind for s in out] == ["write_hunk", "edit", "write_hunk", "write_hunk"]
    assert [s.id for s in out] == [1, 2, 3, 4]
    files = {}
    for s in out:
        files[s.file] = apply(files.get(s.file, base.get(s.file)), s)
    assert files == {"app.py": "import sys\n\nDEBUG = True\nPORT = 8080\n", "new.py": "x = 1\n"}


def test_reconcile_adds_unattributed_for_bash_edits_and_drops_broken_steps():
    base = {"a.py": "A = 1\n", "b.py": "B = 1\n", "tests/test_a.py": "t\n"}
    commit = {"a.py": "A = 2\n", "b.py": "B = 1\nB2 = 2\n", "c.py": "C = 1\n", "tests/test_a.py": "t2\n"}
    steps = [mk(1, "a.py", "edit", "A = 1", "A = 2"),
             mk(2, "a.py", "edit", "not there", "x"),          # can't apply: dropped
             mk(3, "tests/test_a.py", "edit", "t", "t2")]     # tests are pinned, kept as-is
    out, fid = reconcile(steps, base.get, commit.get, ["a.py", "b.py", "c.py", "tests/test_a.py"],
                         lambda f: f.startswith("tests/"))
    assert not fid.matches and fid.dropped == [2]
    assert fid.files == ["b.py", "c.py"]
    assert [s.kind for s in out] == ["edit", "edit", "unattributed", "unattributed"]
    assert fid.unattributed == [3, 4]
    files = {}
    for s in out:
        if not s.file.startswith("tests/"):
            files[s.file] = apply(files.get(s.file, base.get(s.file)), s)
    assert files == {k: v for k, v in commit.items() if not k.startswith("tests/")}


def test_reconcile_exact_replay_matches():
    base = {"a.py": "A = 1\n"}
    out, fid = reconcile([mk(1, "a.py", "edit", "A = 1", "A = 2")], base.get, {"a.py": "A = 2\n"}.get,
                         ["a.py"], lambda f: False)
    assert fid.matches and fid.unattributed == [] and len(out) == 1


def test_shell_edits_are_attributed_to_the_agents_command():
    from whyagent.adapters import parse
    from whyagent.adapters.fidelity import attribute_shell_edits
    import json

    def rec(kind, content):
        return json.dumps({"type": kind, "sessionId": "s1", "cwd": "/repo", "timestamp": "2026-11-07T12:00:00Z",
                           "message": {"content": content}})

    lines = [
        rec("user", "<local-command-stdout>Set model to Haiku</local-command-stdout>"),  # not a prompt
        rec("user", "Just change the defaults in config.py."),
        rec("assistant", [{"type": "text", "text": "Lowering the global defaults."},
                          {"type": "tool_use", "id": "b0", "name": "Bash", "input": {"command": "cat payments/config.py"}},
                          {"type": "tool_use", "id": "b1", "name": "Bash", "input": {
                              "command": "sed -i '' 's/^MAX_RETRIES = 3$/MAX_RETRIES = 1/' payments/config.py"}}]),
    ]
    shell = []
    assert parse("\n".join(lines), shell_out=shell) == []      # no Edit calls at all
    assert [c["command"][:6] for c in shell] == ["sed -i"]      # the view-only `cat` isn't recorded
    base, commit = {"payments/config.py": "MAX_RETRIES = 3\n", "other.py": "x\n"}, \
        {"payments/config.py": "MAX_RETRIES = 1\n", "other.py": "y\n"}
    steps, fid = reconcile([], base.get, commit.get, ["payments/config.py", "other.py"], lambda f: False)
    attribute_shell_edits(steps, fid, shell)
    by_file = {s.file: s for s in steps}
    cfg = by_file["payments/config.py"]
    assert cfg.kind == "shell" and cfg.command.startswith("sed -i") and cfg.session_id == "s1"
    assert cfg.prompt == "Just change the defaults in config.py." and cfg.reasoning == "Lowering the global defaults."
    assert by_file["other.py"].kind == "unattributed"           # no command names it: stays unattributed
    assert fid.shell == [cfg.id] and fid.unattributed == [by_file["other.py"].id]


def test_modifies_ignores_commits_viewers_and_heredocs():
    from whyagent.adapters.fidelity import _modifies

    f = "payments/config.py"
    sed = ("git checkout -- scripts/smoke_dev.py && git show HEAD:payments/client.py > /tmp/_orig.py; "
           "sed -i '' 's/^TIMEOUT_S = 10$/TIMEOUT_S = 2/' payments/config.py && grep -n TIMEOUT payments/config.py")
    commit = ("git add payments/client.py payments/config.py tests/test_client.py && git commit -q -F - <<'EOF'\n"
              "Fail fast\n\nLower MAX_RETRIES in payments/config.py\nEOF")
    assert _modifies(sed, f)
    assert _modifies(sed, "scripts/smoke_dev.py")            # git checkout -- reverts it
    assert not _modifies(commit, f)                          # git add / commit, and the message, don't count
    assert not _modifies("cat payments/config.py && git diff payments/config.py", f)
    assert _modifies("echo 'X = 1' >> payments/config.py", f)


def _shell_cmd(command, reasoning="r"):
    return {"session_id": "s1", "ts": "t", "command": command, "cwd": "/repo", "reasoning": reasoning,
            "trigger": None, "prompt": "p"}


def _leftovers(base, commit):
    files = sorted(set(base) | set(commit))
    return reconcile([], base.get, commit.get, files, lambda f: False)


def test_shell_attribution_prefers_the_command_that_wrote_the_content():
    from whyagent.adapters.fidelity import attribute_shell_edits

    # the sed makes the change; a later `git diff`/`git add` and a restore of another file also name it
    shell = [_shell_cmd("sed -i 's/^MAX_RETRIES = 3$/MAX_RETRIES = 1/' payments/config.py", "lower retries"),
             _shell_cmd("python -m pytest -q && git checkout -- payments/config.py.bak", "check"),
             _shell_cmd("echo '# tuned' >> payments/config.py", "comment")]
    steps, fid = _leftovers({"payments/config.py": "MAX_RETRIES = 3\n"}, {"payments/config.py": "MAX_RETRIES = 1\n"})
    attribute_shell_edits(steps, fid, shell)
    (s,) = steps
    assert s.kind == "shell" and s.command.startswith("sed -i") and s.reasoning == "lower retries"
    assert fid.shell_guessed == []


def test_shell_attribution_splits_hunks_between_commands():
    from whyagent.adapters.fidelity import attribute_shell_edits

    base = {"c.py": "A = 1\n" + "x\n" * 10 + "B = 1\n"}
    commit = {"c.py": "A = 2\n" + "x\n" * 10 + "B = 2\n"}
    shell = [_shell_cmd("sed -i 's/A = 1/A = 2/' c.py", "a"), _shell_cmd("sed -i 's/B = 1/B = 2/' c.py", "b")]
    steps, fid = _leftovers(base, commit)
    attribute_shell_edits(steps, fid, shell)
    assert [s.reasoning for s in steps] == ["a", "b"] and fid.shell_guessed == []


def test_shell_attribution_flags_a_guess_when_no_command_shows_the_content():
    from whyagent.adapters.fidelity import attribute_shell_edits

    shell = [_shell_cmd("python scripts/migrate.py config.py", "first"),
             _shell_cmd("python scripts/tidy.py config.py", "second")]
    steps, fid = _leftovers({"config.py": "a\n"}, {"config.py": "b\n"})
    attribute_shell_edits(steps, fid, shell)
    (s,) = steps
    assert s.kind == "shell" and s.reasoning == "second"  # last one wins...
    assert fid.shell_guessed == [s.id]                    # ...but it's flagged as a guess


def test_shell_attribution_one_candidate_is_not_a_guess():
    from whyagent.adapters.fidelity import attribute_shell_edits

    steps, fid = _leftovers({"config.py": "a\n"}, {"config.py": "b\n"})
    attribute_shell_edits(steps, fid, [_shell_cmd("python scripts/migrate.py config.py")])
    assert steps[0].kind == "shell" and fid.shell_guessed == []
