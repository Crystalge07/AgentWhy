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
