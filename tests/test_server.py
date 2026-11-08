import json
import sys

from whyagent import events
from whyagent.server import Results


def test_results_list_and_event_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.stdout)  # events.start tees stdout: undo it after the test
    monkeypatch.setattr(events, "_file", None)
    hist = tmp_path / "history"
    events.start(hist / "abc123", id="abc123", repo="demo", test="pytest t.py::test_x")
    events.emit("state_start", stage="precheck", key="k", base="aaaaaaa", edits=0, runs=5)
    r = Results(hist)
    [row] = r.list()
    assert row["id"] == "abc123" and row["status"] == "running" and row["test"] == "test_x"

    first = r.events("abc123", 0)
    assert [e["type"] for e in first["events"]] == ["meta", "state_start"]
    events.emit("done", code=0)
    (hist / "abc123" / "report.json").write_text(json.dumps({"meta": {"test": "pytest t.py::test_x"}}), encoding="utf-8")
    more = r.events("abc123", first["next"])
    assert [e["type"] for e in more["events"]] == ["done"] and more["status"] == "done"
    assert r.events("../etc", 0)["status"] == "missing"  # ids can't escape the history folder
