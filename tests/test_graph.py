from whyagent.graph import named_test_files, prune_candidates, reachable
from whyagent.types import Step

REPO = {
    "payments/__init__.py": "",
    "payments/config.py": "X = 1\n",
    "payments/client.py": "from . import config\nfrom .models import Invoice\nimport payments.transport\n",
    "payments/models.py": "",
    "payments/transport.py": "import requests\n",
    "payments/export.py": "from .money import format_cents\n",
    "payments/money.py": "",
    "payments/unused.py": "",
    "scripts/tool.py": "from payments import export\n",
    "tests/conftest.py": "from payments.client import PaymentsClient\n",
    "tests/test_client.py": "from conftest import FakeApi\nfrom payments import config\n",
    "settings.json": "{}",
}


def test_named_test_files_in_command():
    files = set(REPO)
    assert named_test_files("pytest -q tests/test_client.py::test_get_invoice", files) == ["tests/test_client.py"]
    assert named_test_files("python -m pytest -k charge", files) == []


def test_reachable_follows_imports_relative_imports_and_conftest():
    r = reachable("pytest tests/test_client.py::test_x", set(REPO), REPO.get)
    assert r == {"tests/test_client.py", "tests/conftest.py", "payments/__init__.py", "payments/config.py",
                 "payments/client.py", "payments/models.py", "payments/transport.py"}
    assert reachable("pytest -k x", set(REPO), REPO.get) is None


def test_prune_candidates_keeps_non_python_and_dependencies():
    def s(i, f):
        return Step(i, "s", "claude-code", f, "edit", "a", "b")

    steps = [s(1, "payments/money.py"), s(2, "payments/money.py"), s(3, "payments/client.py"),
             s(4, "settings.json"), s(5, "payments/unused.py")]
    reach = reachable("pytest tests/test_client.py", set(REPO), REPO.get)
    dep = {1: set(), 2: {1}, 3: set(), 4: set(), 5: set()}
    assert prune_candidates(steps, [1, 2, 3, 4, 5], reach, dep) == [1, 2, 5]
    # a kept (reachable) edit depending on a candidate protects it
    dep[3] = {5}
    assert prune_candidates(steps, [1, 2, 3, 4, 5], reach, dep) == [1, 2]
