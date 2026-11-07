import pytest

from whyagent import entire

OUT = """
    def test_create_charge(client):
>       ch = client.create_charge("inv_001", 1250)
E       payments.client.ApiError: POST /charges: gave up after 3 attempts: refusing to POST https://x/v1/charges without TLS verification
payments/client.py:36: ApiError
"""


@pytest.fixture(autouse=True)
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("WHYAGENT_BRAIN", str(tmp_path / "brain.jsonl"))


def lesson(**kw):
    base = {"text": "Never disable TLS verification; point PAYMENTS_CA_BUNDLE at the dev cert.",
            "files": ["payments/config.py"], "failing_output": OUT, "verdict": "VALIDATED", "p": 0.004}
    return {**base, **kw}


def test_error_key_normalizes():
    assert entire.error_key(OUT) == ("ApiError", entire.error_key(OUT.replace("3 attempts", "5 attempts"))[1])
    assert entire.error_key(OUT)[0] == "ApiError"
    assert entire.error_key("all good") == ("", "")


def test_save_then_lookup_by_file_and_error_type():
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


def test_only_validated_lessons_are_saved():
    with pytest.raises(ValueError, match="validated"):
        entire.brain_save(lesson(verdict="PROMISING"))
    with pytest.raises(ValueError, match="missing"):
        entire.brain_save({"verdict": "VALIDATED", "text": "x"})
    assert not entire.brain_path().exists()
