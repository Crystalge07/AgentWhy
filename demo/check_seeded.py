"""Check that the seeded demo repo can produce all three demo bugs, before anyone records sessions.

Plants a hand-written version of each scenario's mistake in a throwaway copy of the repo and
checks that it fails the target test in the way the demo needs. The seeded repo itself never
contains these patches, so recorded agents can't see them.

    python demo/check_seeded.py ../payments-demo [--flaky-runs 30]
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Each scenario: (file, old, new) replacements that plant the bug, the target test, and what to expect.
SCENARIOS = {
    "1-pagination": {
        "patch": [("payments/client.py",
                   '''    def list_invoices(self) -> list[Invoice]:
        return [Invoice.from_dict(d) for d in self._request("GET", "/invoices")["data"]]''',
                   '''    def list_invoices(self, page_size: int | None = None) -> list[Invoice]:
        size = page_size or config.PAGE_SIZE
        first = self._request("GET", f"/invoices?page=1&per_page={size}")
        pages = first["total"] // size
        items = list(first["data"])
        for page in range(2, pages + 1):
            items += self._request("GET", f"/invoices?page={page}&per_page={size}")["data"]
        return [Invoice.from_dict(d) for d in items]''')],
        "test": "tests/test_export.py::test_export_includes_every_invoice",
        "expect": "stable fail",
    },
    "2-tls": {
        "patch": [("payments/config.py", "VERIFY_TLS = True", "VERIFY_TLS = False")],
        "test": "tests/test_client.py::test_create_charge",
        "expect": "stable fail",
        # the traceback's deepest frame must be the retry loop in client.py, not config.py
        "frame": ("payments/client.py", "gave up after"),
    },
    "3-cache": {
        "patch": [("payments/client.py",
                   '''    def get_invoice(self, invoice_id: str) -> Invoice:
        return Invoice.from_dict(self._request("GET", f"/invoices/{invoice_id}"))''',
                   '''    _cache: dict = {}
    CACHE_TTL_S = 1.0

    def get_invoice(self, invoice_id: str) -> Invoice:
        hit = self._cache.get(invoice_id)
        if hit and time.monotonic() - hit[0] < self.CACHE_TTL_S:
            return hit[1]
        inv = Invoice.from_dict(self._request("GET", f"/invoices/{invoice_id}"))
        self._cache[invoice_id] = (time.monotonic(), inv)
        return inv''')],
        "test": "tests/test_settlement.py::test_invoice_paid_after_charge_settles",
        "expect": "flaky",
    },
}


def venv_python(repo: Path) -> str:
    for p in (repo / ".venv" / "Scripts" / "python.exe", repo / ".venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    return sys.executable


def pytest(py: str, cwd: Path, test: str = "") -> subprocess.CompletedProcess:
    args = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=long"] + ([test] if test else [])
    return subprocess.run(args, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=120)


def copy_repo(repo: Path) -> Path:
    d = Path(tempfile.mkdtemp(prefix="seeded-"))
    shutil.copytree(repo, d / "repo", ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "dev"))
    return d / "repo"


def plant(root: Path, patch):
    for rel, old, new in patch:
        p = root / rel
        text = p.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(f"patch target not found exactly once in {rel}; did the seeded repo change?")
        p.write_text(text.replace(old, new), encoding="utf-8")


_FRAME = re.compile(r"^([\w./\\-]+\.py):(\d+): \w+")


def deepest_frame(output: str) -> str:
    """Last 'path.py:line' frame location pytest prints in a --tb=long traceback."""
    locs = [m[0].rsplit(":", 1)[0] for ln in output.splitlines() if (m := _FRAME.match(ln))]
    return locs[-1] if locs else ""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252, which can't print ✓/✗
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--flaky-runs", type=int, default=30)
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    py = venv_python(repo)
    ok_all = True

    base = copy_repo(repo)
    res = [pytest(py, base) for _ in range(5)]
    passed = sum(r.returncode == 0 for r in res)
    ok = passed == 5
    ok_all &= ok
    print(f"{'✓' if ok else '✗'} baseline        full suite passes {passed}/5")
    shutil.rmtree(base.parent, ignore_errors=True)

    for name, sc in SCENARIOS.items():
        root = copy_repo(repo)
        plant(root, sc["patch"])
        n = args.flaky_runs if sc["expect"] == "flaky" else 3
        runs = [pytest(py, root, sc["test"]) for _ in range(n)]
        fails = sum(r.returncode != 0 for r in runs)
        if sc["expect"] == "stable fail":
            ok = fails == n
            detail = f"target test fails {fails}/{n}"
        else:
            ok = 0.15 * n <= fails <= 0.85 * n
            detail = f"target test fails {fails}/{n} (want 15–85%)"
        if ok and "frame" in sc:
            out = next(r.stdout for r in runs if r.returncode != 0)
            frame = deepest_frame(out)
            want_file, want_text = sc["frame"]
            ok = frame.replace("\\", "/").startswith(want_file) and want_text in out
            detail += f"; deepest frame {frame or '?'}"
        # the rest of the suite must not crash at import time (that would make every state OTHER)
        full = pytest(py, root)
        if "error" in full.stdout.splitlines()[-1].lower() if full.stdout else True:
            ok, detail = False, detail + "; suite has collection errors"
        ok_all &= ok
        print(f"{'✓' if ok else '✗'} {name:<15} {detail}")
        shutil.rmtree(root.parent, ignore_errors=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
