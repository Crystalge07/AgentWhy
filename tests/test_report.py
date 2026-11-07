import json
from pathlib import Path

from whyagent.report import Report, render_html
from whyagent.types import RunResult

SAMPLE = Path(__file__).parent.parent / "examples" / "sample_report.json"


def test_render_embeds_data_and_escapes_script_close():
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["notes"].append("</script><b>x</b>")
    html = render_html(data)
    assert "/*__WHYAGENT_DATA__*/" not in html
    assert "</script><b>" not in html
    payload = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(payload)["root_cause"]["step_id"] == 3


def test_report_write(tmp_path):
    r = Report(test="pytest t.py", good="a" * 40, head="b" * 40)
    r.log_runs("precheck", "bbbbbbb", [RunResult("TARGET_FAIL", "sig", 1, 10)])
    r["precheck"] = {"head_fail": 3, "good_pass": 3, "n": 3, "mode": "stable"}
    out = r.write(tmp_path / "r.html")
    assert out.exists() and (tmp_path / "r.json").exists()
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["runs"][0]["outcome"] == "TARGET_FAIL"
