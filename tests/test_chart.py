import json

from whyagent.chart import render

ROWS = [
    {"label": "payments-demo: pytest tests/test_client.py::test_create_charge", "pruned": False,
     "analysis_id": "a1", "started": "2026-11-08T10:00+00:00",
     "stages": {"precheck": 6, "commit": 2, "blame hint": 1, "bisect": 4, "confirm": 1}},
    {"label": "payments-demo: pytest tests/test_client.py::test_create_charge", "pruned": True,
     "analysis_id": "a2", "started": "2026-11-08T10:05+00:00",
     "stages": {"precheck": 6, "commit": 2, "blame hint": 1, "pruning": 1, "bisect": 2, "confirm": 1}},
    {"label": "payments-demo: pytest tests/test_export.py::test_export_includes_every_invoice", "pruned": True,
     "analysis_id": "a3", "started": "2026-11-08T10:10+00:00", "stages": {"precheck": 6, "commit": 2, "blame hint": 1}},
]


def test_render_chart():
    html = render(ROWS + [{"label": "</script>x", "pruned": True, "analysis_id": "z", "started": "", "stages": {}}])
    payload = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    assert len(json.loads(payload)["rows"]) == 4
    assert "</script>x" not in payload


def test_bug_name_pairs_different_spellings_of_the_same_test():
    from whyagent.chart import bug_name
    a = bug_name("payments-demo: pytest -q tests/test_client.py::test_x")
    b = bug_name("payments-demo: /Users/me/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_client.py::test_x")
    c = bug_name("payments-demo: C:\\v\\python.exe -m pytest tests\\test_client.py::test_x")
    assert a == b == c == "payments-demo: tests/test_client.py::test_x"
    assert bug_name("repo: make test") == "repo: make test"


def test_fetch_keeps_latest_edit_level_analysis_per_bug_and_mode():
    from datetime import datetime, timezone
    from whyagent.chart import fetch

    t = lambda m: datetime(2026, 11, 7, 19, m, tzinfo=timezone.utc)
    L = "demo: python -m pytest tests/t.py::test_a"
    rows = [(L, False, "old", t(1), "bisect", 9, 0),
            (L, False, "new", t(2), "precheck", 10, 0), (L, False, "new", t(2), "bisect", 3, 0),
            (L, False, "blamehit", t(3), "blame_hint", 1, 0),          # no edit-level search: skipped
            ("demo: pytest tests/t.py::test_a", True, "p", t(4), "prune", 1, 0),
            ("demo: pytest tests/t.py::test_a", True, "p", t(4), "bisect", 2, 0)]

    class Conn:
        def execute(self, sql, *a):
            if sql.startswith("CALL"):
                raise RuntimeError("no aggregate")
            return type("R", (), {"fetchall": lambda _self: rows})()

    got = {(d["label"], d["pruned"]): d["analysis_id"] for d in fetch(Conn())}
    assert got == {("demo: tests/t.py::test_a", False): "new", ("demo: tests/t.py::test_a", True): "p"}
