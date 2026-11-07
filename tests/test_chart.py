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
