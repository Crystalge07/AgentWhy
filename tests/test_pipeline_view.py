"""The pipeline view's data: the event hook, the replay loader, the SSE stream, and the decoy story in events."""
import json
import sys

import pytest

from whyagent import events
from whyagent.events import load_replay
from whyagent.pipeline import Options, find_root_cause
from whyagent.replay import LocalSandbox, Replayer
from whyagent.report import Report
from whyagent.server import Results, stream
from whyagent.types import State
from whyagent.adapters.fidelity import Fidelity

from test_pipeline import make_repo


@pytest.fixture
def recording(tmp_path, monkeypatch):
    """events.start() into a temp history folder, undone afterwards (it tees stdout)."""
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(events, "_file", None)
    folder = tmp_path / "history" / "run1"
    events.start(folder, id="run1", test="pytest t.py::test_x")
    yield folder
    events._file.close()
    events._file = None


def read(folder):
    return [json.loads(ln) for ln in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_hook_is_a_noop_by_default(monkeypatch):
    monkeypatch.setattr(events, "_file", None)
    events.stage("bisect", kept=[1, 2])  # nothing listening: no file, no error
    events.emit("state_start", stage="bisect")
    assert not events.active()


def test_stage_events_are_recorded(recording):
    events.stage("bisect", kept=[1, 2, 3])
    events.stage("bisect", "done", boundary=2)
    evs = read(recording)
    assert [(e["type"], e.get("name"), e.get("status")) for e in evs] == [
        ("meta", None, None), ("stage", "bisect", "start"), ("stage", "bisect", "done")]
    assert evs[1]["kept"] == [1, 2, 3] and evs[2]["boundary"] == 2 and all("t" in e for e in evs)


def test_load_replay_caps_gaps_and_pauses_after_results(tmp_path):
    p = tmp_path / "events.jsonl"
    rows = [{"t": 100.0, "type": "meta"}, {"t": 100.5, "type": "line", "text": "a"},
            {"t": 160.0, "type": "state_start"},            # a 60 s Docker build: capped
            {"t": 160.1, "type": "state_done", "outcomes": ["PASS"]},
            {"t": 160.2, "type": "line", "text": "b"}]      # right after a result: gets the pause
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n{half a line", encoding="utf-8")
    evs = load_replay(p, max_gap=3.0, pause=0.8)
    assert [e["type"] for e in evs] == ["meta", "line", "state_start", "state_done", "line"]  # bad line skipped
    assert [e["delay"] for e in evs] == [0.0, 0.5, 3.0, 0.1, 0.8]
    assert evs[1]["text"] == "a"  # the event itself is untouched


def test_stream_sends_everything_then_stops_at_done(recording):
    events.stage("precheck")
    events.emit("done", code=0)
    sent = []
    stream(Results(recording.parent), "run1", sent.append, poll=0)
    datas = [json.loads(b[len(b"data: "):]) for b in sent if b.startswith(b"data: ")]
    assert [d["type"] for d in datas] == ["meta", "stage", "done"]
    assert all(b.endswith(b"\n\n") for b in sent)


def test_stream_of_unknown_run_ends(tmp_path):
    sent = []
    stream(Results(tmp_path / "history"), "nope", sent.append, poll=0)
    assert sent and sent[-1].startswith(b"event: end")


def test_decoy_story_in_events(tmp_path, recording):
    """What the view draws for the demo's key moment: blame suspects edit #4, the revert still fails, and the
    bisect lands on edit #3, with every test tagged by the edits it applied."""
    parent, g, steps, files = make_repo(tmp_path)
    cmd = f'"{sys.executable}" -m pytest -q -p no:cacheprovider tests/test_core.py::test_total'
    head_run = Replayer(str(tmp_path), [], cmd).run(State(g, [], g))[0]
    decoy_line = files["calc/core.py"].splitlines().index("    return result") + 1
    rp = Replayer(str(tmp_path), steps, cmd, target_sig=head_run.signature, sandbox=LocalSandbox())
    rc = find_root_cause(str(tmp_path), steps, Fidelity(matches=True), g, parent, g, rp,
                         Options(line=f"calc/core.py:{decoy_line}"), Report(), say=lambda *_: None)
    assert rc.step.id == 3
    evs = read(recording)
    stages = [(e["name"], e["status"]) for e in evs if e["type"] == "stage"]
    assert stages == [("session", "start"), ("session", "done"), ("blame_hint", "start"), ("blame_hint", "done"),
                      ("prune", "start"), ("prune", "done"), ("bisect", "start"), ("bisect", "done"),
                      ("confirm", "start"), ("confirm", "done")]
    by = {(e["name"], e["status"]): e for e in evs if e["type"] == "stage"}
    assert by[("blame_hint", "start")]["suspect"] == 4 and by[("blame_hint", "done")]["is_cause"] is False
    assert by[("bisect", "done")]["boundary"] == 3 and by[("confirm", "done")]["root"] == 3
    [cards] = [e for e in evs if e["type"] == "steps"]
    assert [c["id"] for c in cards["steps"]] == [1, 2, 3, 4, 5, 6] and cards["steps"][2]["change"] == "RATE = 11"
    bisect_tests = [e for e in evs if e["type"] == "state_done" and e["stage"] == "bisect"]
    assert bisect_tests and all(e["ids"] == by[("bisect", "start")]["kept"][:len(e["ids"])] for e in bisect_tests)
    [root] = [e for e in evs if e["type"] == "root_cause"]
    assert root["step_id"] == 3 and root["file"] == "calc/config.py"
