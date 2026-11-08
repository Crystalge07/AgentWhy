import json

import pytest

import whyagent.tiger
from whyagent import entire

OUT = """
    def test_create_charge(client):
>       ch = client.create_charge("inv_001", 1250)
E       payments.client.ApiError: POST /charges: gave up after 3 attempts: refusing to POST https://x/v1/charges without TLS verification
payments/client.py:36: ApiError
"""


class FakeTiger:
    """The `lessons` table, in memory. Never touches the real shared store."""

    def __init__(self):
        self.table: list[dict] = []
        self.fail = False

    def __enter__(self):
        if self.fail:
            raise ConnectionError("Tiger is down")
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=()):
        if sql.startswith("INSERT"):
            self.table.append(json.loads(args[0]))
        return type("R", (), {"fetchall": lambda _s: [(r,) for r in self.table]})()


@pytest.fixture(autouse=True)
def tiger(tmp_path, monkeypatch):
    fake = FakeTiger()
    monkeypatch.setenv("TIGER_DATABASE_URL", "postgres://fake")
    monkeypatch.setattr(whyagent.tiger, "connect", lambda url=None, **kw: fake)
    monkeypatch.setenv("HOME", str(tmp_path))  # a local Brain file, if anything wrote one, would land here
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return fake


def lesson(**kw):
    base = {"text": "Never disable TLS verification; point PAYMENTS_CA_BUNDLE at the dev cert.",
            "files": ["payments/config.py"], "failing_output": OUT, "verdict": "VALIDATED", "p": 0.004}
    return {**base, **kw}


def test_error_key_normalizes():
    assert entire.error_key(OUT) == ("ApiError", entire.error_key(OUT.replace("3 attempts", "5 attempts"))[1])
    assert entire.error_key(OUT)[0] == "ApiError"
    assert entire.error_key("all good") == ("", "")


def test_save_then_lookup_by_file_and_error_type(tiger):
    assert entire.brain_lookup(["payments/config.py"], OUT) is None
    entire.brain_save(lesson())
    hit = entire.brain_lookup(["payments/config.py", "payments/client.py"], OUT)
    assert hit and hit["text"].startswith("Never disable TLS") and hit["error_type"] == "ApiError"
    assert "failing_output" not in hit
    # a different file and a different error don't match
    assert entire.brain_lookup(["payments/export.py"], "E   AssertionError: assert 20 == 25") is None


def test_identical_error_matches_even_in_another_file():
    entire.brain_save(lesson())
    assert entire.brain_lookup(["other/thing.py"], OUT) is not None


def test_only_validated_lessons_are_saved(tiger):
    with pytest.raises(ValueError, match="validated"):
        entire.brain_save(lesson(verdict="PROMISING"))
    with pytest.raises(ValueError, match="missing"):
        entire.brain_save({"verdict": "VALIDATED", "text": "x"})
    assert tiger.table == []


def test_lessons_live_only_in_tiger(tiger, tmp_path):
    """Saved to Tiger and nowhere else, so a lesson deleted there is gone for every teammate."""
    entire.brain_save(lesson())
    assert len(tiger.table) == 1 and len(entire._brain_records()) == 1
    assert not any(tmp_path.rglob("*.jsonl"))  # no local copy
    tiger.table.clear()  # deleted in Tiger: no machine finds it any more
    assert entire.brain_lookup(["payments/config.py"], OUT) is None


def test_save_fails_loudly_without_tiger(tiger, monkeypatch):
    monkeypatch.delenv("TIGER_DATABASE_URL")
    with pytest.raises(RuntimeError, match="TIGER_DATABASE_URL"):
        entire.brain_save(lesson())
    assert entire.brain_lookup(["payments/config.py"], OUT) is None  # lookups just find nothing
    monkeypatch.setenv("TIGER_DATABASE_URL", "postgres://fake")
    tiger.fail = True
    with pytest.raises(ConnectionError):
        entire.brain_save(lesson())
    assert entire.brain_lookup(["payments/config.py"], OUT) is None
