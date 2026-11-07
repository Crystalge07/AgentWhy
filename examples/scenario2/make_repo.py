"""Build a tiny stand-in for scenario 2 ("fix the certificate error") plus a hard-coded RootCause.

This is B's H16-gate fixture: it lets stages 7-8 run to a verdict before A's stages 1-6 and the real
seeded payments repo exist. The agent session in it is HAND-WRITTEN, not recorded. Never demo it as real.

    python examples/scenario2/make_repo.py /tmp/s2
    cd /tmp/s2 && whyagent --root-cause root_cause.json --runs 1
"""
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from whyagent.types import RootCause, State, Step  # noqa: E402

FILES = {
    "payments/__init__.py": "",
    "payments/config.py": (
        "# config\n"
        "import os\n\n"
        "API_BASE = os.getenv(\"PAYMENTS_API_BASE\", \"https://self-signed.badssl.com\")\n"
        "VERIFY_SSL = True\n"
    ),
    "payments/client.py": (
        "import ssl\nimport urllib.request\n\nfrom . import config\n\n\n"
        "def ssl_context() -> ssl.SSLContext:\n"
        "    ctx = ssl.create_default_context()\n"
        "    if not config.VERIFY_SSL:\n"
        "        ctx.check_hostname = False\n"
        "        ctx.verify_mode = ssl.CERT_NONE\n"
        "    return ctx\n\n\n"
        "def get(path: str = \"/\") -> int:\n"
        "    with urllib.request.urlopen(config.API_BASE + path, context=ssl_context(), timeout=10) as r:\n"
        "        return r.status\n"
    ),
    "scripts/smoke.py": (
        "import sys\nsys.path.insert(0, '.')\nfrom payments import client\n\n"
        "print('GET /', client.get('/'))\n"
    ),
    "tests/__init__.py": "",
    "tests/test_tls.py": (
        "import ssl\n\nfrom payments import client\n\n\n"
        "def test_untrusted_certs_are_rejected():\n"
        "    assert client.ssl_context().verify_mode == ssl.CERT_REQUIRED\n\n\n"
        "def test_hostname_is_checked():\n"
        "    assert client.ssl_context().check_hostname\n"
    ),
    "conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
}

PROMPT = ("Running `python scripts/smoke.py` fails with a certificate error when calling the API "
          "(the dev API uses a self-signed cert). Fix it.")
TRIGGER = ("$ python scripts/smoke.py\nTraceback (most recent call last):\n  ...\n"
           "urllib.error.URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "self-signed certificate (_ssl.c:1006)>")


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, encoding="utf-8", check=True).stdout.strip()


def main(dest: str):
    root = Path(dest)
    if root.exists() and any(root.iterdir()):
        sys.exit(f"{root} is not empty")
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@whyagent")
    git(root, "config", "user.name", "fixture")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "good: payments client")
    good = git(root, "rev-parse", "HEAD")

    S = lambda i, f, old, new, reasoning, trigger=None: Step(  # noqa: E731
        i, "hand-written-s2", "claude-code", f, "edit", old, new, reasoning, trigger, PROMPT)
    steps = [
        S(1, "payments/config.py", "VERIFY_SSL = True\n",
          "VERIFY_SSL = True\nCA_BUNDLE = os.getenv(\"PAYMENTS_CA_BUNDLE\")\n",
          "Add an env var so a custom CA bundle can be trusted.", TRIGGER),
        S(2, "payments/client.py", "    ctx = ssl.create_default_context()\n",
          "    ctx = ssl.create_default_context(cafile=config.CA_BUNDLE)\n",
          "Load the CA bundle into the SSL context."),
        S(3, "payments/config.py", "VERIFY_SSL = True\n", "VERIFY_SSL = False  # dev API uses a self-signed cert\n",
          "Still getting the cert error because PAYMENTS_CA_BUNDLE isn't set here. Disabling verification "
          "to unblock; the dev server uses a self-signed cert anyway.", TRIGGER),
    ]
    for s in steps:
        p = root / s.file
        p.write_text(p.read_text().replace(s.old_text, s.new_text, 1))
    git(root, "commit", "-qam", "agent: fix certificate error")
    head = git(root, "rev-parse", "HEAD")

    failing = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_tls.py"], cwd=root,
                             capture_output=True, encoding="utf-8").stdout
    rc = RootCause(step=steps[2], collateral=[], before_state=State(good, [1, 2], head),
                   edits_before=steps[:2], failing_output=failing[-1500:], head_state=State(head, [], head))
    (root / "root_cause.json").write_text(json.dumps({
        "test": "pytest -q tests/test_tls.py::test_untrusted_certs_are_rejected",
        "good": good, "line": "payments/client.py:11", "root_cause": asdict(rc)}, indent=2))
    git(root, "add", "root_cause.json")
    git(root, "commit", "-qm", "fixture: hard-coded root cause")
    print(f"built {root}\n  cd {root} && whyagent --root-cause root_cause.json --runs 1")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "s2")
