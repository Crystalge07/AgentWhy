from whyagent.bisect import closure, confirm, ddmin, deps, prefix_bisect
from whyagent.types import OTHER, PASS, TARGET_FAIL, Step


def mk(i, f, old, new):
    return Step(i, "s", "claude-code", f, "edit", old, new)


def test_prefix_bisect_finds_boundary():
    order = list(range(1, 16))
    calls = []

    def run(ids):
        calls.append(len(ids))
        return TARGET_FAIL if 3 in ids else PASS

    assert order[prefix_bisect(order, run) - 1] == 3
    assert len(calls) <= 6


def test_prefix_bisect_skips_other():
    order = list(range(1, 9))

    def run(ids):
        if len(ids) == 2:
            return OTHER
        return TARGET_FAIL if 5 in ids else PASS

    assert order[prefix_bisect(order, run) - 1] == 5


def test_prefix_bisect_other_at_boundary_is_inconclusive():
    run = lambda ids: OTHER if len(ids) == 4 else (TARGET_FAIL if 5 in ids else PASS)
    assert prefix_bisect(list(range(1, 9)), run) is None


def test_deps_and_confirm():
    steps = [mk(1, "a.py", "x = 1", "x = 2"), mk(2, "a.py", "x = 2", "x = 3"), mk(3, "b.py", "y", "z")]
    d = deps(steps, base={"a.py": "x = 1\n", "b.py": "y\n"})
    assert d[2] == {1} and d[3] == set()
    assert closure({1}, d) == {1, 2}
    ok, collateral = confirm([1, 2, 3], 1, d, lambda ids: PASS if 1 not in ids else TARGET_FAIL)
    assert ok and collateral == {2}


def test_ddmin_two_causes():
    final = list(range(1, 9))
    run = lambda ids: PASS if (2 not in ids and 6 not in ids) else TARGET_FAIL
    assert ddmin(final, {i: set() for i in final}, run, cap=50) == {2, 6}


from whyagent.bisect import blame_hint, line_owners, map_line


def test_map_line_follows_shifts_and_drops_changed_lines():
    g = "a\nb\nc\n"
    head = "new\na\nb\nc changed\n"
    assert map_line(head, g, 2) == 1      # "a" moved down one line at HEAD
    assert map_line(head, g, 3) == 2
    assert map_line(head, g, 4) is None   # changed after G: can't map


def test_line_owners_tracks_last_writer():
    base = "def f():\n    return 1\n\nX = 0\n"
    steps = [mk(1, "a.py", "    return 1", "    y = 1\n    return y"),
             mk(2, "b.py", "q", "r"),
             mk(3, "a.py", "X = 0", "X = 5")]
    # final: def f(): / y = 1 / return y / (blank) / X = 5
    assert line_owners(steps, "a.py", base) == [None, 1, 1, None, 3]


def _scenario(cause, blamed):
    """10 edits; `cause` breaks the test; edit 9 depends on edit 8."""
    order = list(range(1, 11))
    dep = {i: set() for i in order}
    dep[9] = {8}
    calls = []

    def run(ids):
        calls.append(list(ids))
        return TARGET_FAIL if cause in ids else PASS

    return blame_hint(order, blamed, dep, run), calls


def test_blame_hint_confirmed_in_three_runs():
    r, calls = _scenario(cause=7, blamed=7)
    assert r["is_cause"] and r["before"] == PASS and r["after"] == TARGET_FAIL
    assert len(calls) == 3


def test_blame_hint_not_the_cause():
    r, calls = _scenario(cause=3, blamed=8)
    assert not r["is_cause"] and r["outcome"] == TARGET_FAIL
    assert r["reverted"] == [8, 9]    # dependents are reverted with it
    assert len(calls) == 1


from whyagent.bisect import prefix_bisect_parallel, scan, sequential


def test_parallel_bisect_two_rounds_for_15_edits_with_4_workers():
    order = list(range(1, 16))
    for cause in order:
        batches = []

        def run_many(prefixes, cause=cause):
            batches.append(len(prefixes))
            return [TARGET_FAIL if cause in p else PASS for p in prefixes]

        k, rounds = prefix_bisect_parallel(order, run_many, workers=4)
        assert order[k - 1] == cause
        assert rounds <= 2 and max(batches) <= 4


def test_parallel_bisect_skips_other_and_reports_inconclusive():
    order = list(range(1, 9))
    run = lambda ids: OTHER if len(ids) == 3 else (TARGET_FAIL if 5 in ids else PASS)  # not at the boundary
    assert prefix_bisect_parallel(order, sequential(run), workers=2)[0] == 5
    run2 = lambda ids: OTHER if len(ids) == 4 else (TARGET_FAIL if 5 in ids else PASS)
    assert prefix_bisect_parallel(order, sequential(run2), workers=2)[0] is None


def test_parallel_bisect_one_worker_matches_sequential():
    order = list(range(1, 16))
    run = lambda ids: TARGET_FAIL if 11 in ids else PASS
    assert prefix_bisect_parallel(order, sequential(run), workers=1)[0] == prefix_bisect(order, run)


def test_scan_finds_every_boundary_when_the_test_flips():
    order = list(range(1, 9))
    # fails after edit 2, fixed by edit 4, broken again by edit 6 (OTHER at 7 is ignored)
    out = {1: PASS, 2: TARGET_FAIL, 3: TARGET_FAIL, 4: PASS, 5: PASS, 6: TARGET_FAIL, 7: OTHER, 8: TARGET_FAIL}
    r = scan(order, sequential(lambda ids: out[len(ids)]))
    assert r["boundaries"] == [2, 6] and r["first"] == 2 and r["flips"]


def test_commit_bisect_skips_other_commits():
    """cli uses prefix_bisect over commits: a crashing commit (OTHER) is never blamed."""
    commits = ["c1", "c2", "c3", "c4", "c5"]
    outcome = {"c1": PASS, "c2": OTHER, "c3": PASS, "c4": TARGET_FAIL, "c5": TARGET_FAIL}
    k = prefix_bisect(commits, lambda prefix: outcome[prefix[-1]])
    assert commits[k - 1] == "c4"
    # right at the boundary, an OTHER commit could be the guilty one: inconclusive, not a guess
    outcome.update(c3=OTHER, c2=PASS)
    assert prefix_bisect(commits, lambda prefix: outcome[prefix[-1]]) is None
