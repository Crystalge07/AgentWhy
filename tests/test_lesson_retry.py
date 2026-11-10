"""Stage 8 retries: a lesson that doesn't help is replaced by a different one, tested against the same arm A."""
from types import SimpleNamespace

from whyagent import cli
from whyagent.report import Report
from whyagent.rerun.harness import RerunResult
from whyagent.rerun.lesson import build_prompt
from whyagent.types import RootCause, State, Step


def _rc():
    step = Step(2, "sess", "claude-code", "app.py", "edit", "VERIFY = True", "VERIFY = False", prompt="Fix certs")
    return RootCause(step=step, collateral=[], before_state=State("b", [1], "b"), edits_before=[],
                     failing_output="AssertionError", head_state=State("b", [1, 2], "b"))


def test_prompt_lists_failed_lessons_and_what_went_wrong():
    p = build_prompt(_rc().step, "boom", tried=[{"text": "Be careful.", "verdict": "INEFFECTIVE", "a": [0, 5],
                                                 "b": [1, 5], "failures": ["target still fails"]}])
    assert '"Be careful."' in p and "DIFFERENT lesson" in p and "1/5 passed with it, 0/5 without" in p
    assert "target still fails" in p
    assert "DIFFERENT" not in build_prompt(_rc().step, "boom")


class FakeReruns:
    """Arm A always fails; arm B passes only with the lesson in `good`."""
    calls: list = []

    def __init__(self, *a, **kw):
        self.excluded, self.blocked, self._abort, self.capture_diff = [], [], None, False

    def run(self, rc, lesson, n, arm_a=True, job0=0):
        FakeReruns.calls.append((lesson, arm_a))
        out = "PASS" if lesson == "Good lesson." else "FAIL"
        b = [RerunResult("B", out, "target fails" if out == "FAIL" else "ok", 1) for _ in range(n)]
        return ([RerunResult("A", "FAIL", "target fails", 1) for _ in range(n)] if arm_a else []), b


def _run(monkeypatch, lessons, attempts):
    import whyagent.rerun as rerun
    from whyagent import entire, providers

    FakeReruns.calls = []
    seen = []
    queue = list(lessons)
    monkeypatch.setattr(rerun, "Reruns", FakeReruns)
    monkeypatch.setattr(rerun, "agent_by_name", lambda n: SimpleNamespace(name="stub", missing_auth=lambda: None))
    monkeypatch.setattr(providers, "pick_rerun_agent", lambda c: ("stub", None))
    monkeypatch.setattr(cli, "_new_lesson", lambda rc, w, tried=None: (seen.append(tried) or queue.pop(0), "gemini"))
    monkeypatch.setattr(entire, "brain_lookup", lambda *a: None)
    saved = []
    monkeypatch.setattr(entire, "brain_save", saved.append)
    monkeypatch.setattr(entire, "repo_name", lambda r: "repo")
    args = SimpleNamespace(model=None, lesson=None, no_rerun=False, sandbox="docker", rerun_agent="stub", runs=4,
                           workers=1, target_runs=1, pr=False, lesson_attempts=attempts)
    report, out = Report(), {}
    code = cli._lesson_and_reruns(_rc(), SimpleNamespace(repo="."), args, report, None, out)
    return code, report, out, seen, saved


def test_retries_until_a_lesson_validates(monkeypatch):
    code, report, out, seen, saved = _run(monkeypatch, ["Bad one.", "Also bad.", "Good lesson."], 3)
    assert code == 0 and out["verdict"] == "VALIDATED" and out["lesson"] == "Good lesson."
    # arm A once, then only arm B for each new lesson
    assert FakeReruns.calls == [("Bad one.", True), ("Also bad.", False), ("Good lesson.", False)]
    assert seen[0] is None and [t["text"] for t in seen[2]] == ["Bad one.", "Also bad."]
    assert seen[1][0]["failures"] == ["target fails"] * 4
    assert [t["verdict"] for t in report["rerun"]["attempts"]] == ["INEFFECTIVE", "INEFFECTIVE", "VALIDATED"]
    assert report["lesson"]["text"] == "Good lesson." and saved and saved[0]["text"] == "Good lesson."


def test_stops_after_the_attempt_limit(monkeypatch):
    code, report, out, *_ = _run(monkeypatch, ["Bad one.", "Also bad."], 2)
    assert out["verdict"] == "INEFFECTIVE" and len(report["rerun"]["attempts"]) == 2


def test_one_attempt_means_no_retry(monkeypatch):
    _, report, out, *_ = _run(monkeypatch, ["Bad one."], 1)
    assert len(FakeReruns.calls) == 1 and out["verdict"] == "INEFFECTIVE"
