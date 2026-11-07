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
