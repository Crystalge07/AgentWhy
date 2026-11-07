import json
from pathlib import Path

from whyagent.report import Report, render_html
from whyagent.types import RunResult

SAMPLE = Path(__file__).parent.parent / "examples" / "sample_report.json"


def test_render_embeds_data_and_escapes_script_close():
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["notes"].append("</script><b>x</b>")
    html = render_html(data)
    assert "{{ DATA_JSON }}" not in html
    assert "</script><b>" not in html
    payload = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    view = json.loads(payload)
    assert view["report"]["cause"]["edit"] == 3
    assert "</script><b>x</b>" in view["report"]["notes"]
    assert view["db"] is False and view["runs"]


def test_report_write(tmp_path):
    r = Report(test="pytest t.py", good="a" * 40, head="b" * 40)
    r.log_runs("precheck", "bbbbbbb", [RunResult("TARGET_FAIL", "sig", 1, 10)])
    r["precheck"] = {"head_fail": 3, "good_pass": 3, "n": 3, "mode": "stable"}
    out = r.write(tmp_path / "r.html")
    assert out.exists() and (tmp_path / "r.json").exists()
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["runs"][0]["outcome"] == "TARGET_FAIL"


def test_line_from_traceback_takes_the_deepest_non_test_frame():
    from whyagent.cli import line_from_traceback
    out = """
tests/test_client.py:52: in test_retries_server_errors_then_succeeds
    assert client.get_invoice("inv_001").id == "inv_001"
payments/client.py:61: in get_invoice
    return Invoice.from_dict(self._request("GET", f"/invoices/{invoice_id}"))
>       raise ApiError(f"{method} {path}: gave up after {attempts} attempts; "
E       payments.client.ApiError: GET /invoices/inv_001: gave up after 1 attempts

payments/client.py:39: ApiError
=========================== short test summary info ============================
"""
    assert line_from_traceback(out) == "payments/client.py:39"
    # assertion inside the test itself: no non-test frame, so no hint (bisect instead)
    assert line_from_traceback("tests/test_client.py:30: AssertionError\n") is None
    assert line_from_traceback("pkg\\mod.py:7: ValueError\n") == "pkg/mod.py:7"
