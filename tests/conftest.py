import pytest


@pytest.fixture(autouse=True)
def _no_shared_tiger(monkeypatch):
    """Tests never write to the team's shared Tiger Data database.

    Some tests drive the real CLI, which logs every test run to TIGER_DATABASE_URL when it's set (it is, in
    our .env files), so the suite used to add fake 'tests/check.py' analyses to the pruning chart's data.
    """
    # set to empty, not deleted: cli.main() calls load_dotenv(), which would put the real URL back, but it
    # never overrides a variable that already exists (and an empty URL means "don't log")
    monkeypatch.setenv("TIGER_DATABASE_URL", "")
