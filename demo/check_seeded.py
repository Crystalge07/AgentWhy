"""Check that the seeded demo repo can produce all three demo bugs, before anyone records sessions.

Plants a hand-written version of each scenario's mistake in a throwaway copy of the repo and
checks that it fails the target test in the way the demo needs. The seeded repo itself never
contains these patches, so recorded agents can't see them.

    python demo/check_seeded.py ../payments-demo [--ref main] [--flaky-runs 30]

It checks a clean snapshot of --ref (default main), whatever branch payments-demo has checked out.
`--ref WORKTREE` checks the checked-out files instead.
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
    # Scenario 2, non-security version: "make the smoke script fail fast" tempts a global config change
    # (the cause); "make the gave-up error say more" makes the agent rewrite the line the traceback
    # ends on (the decoy). Blame must point at the decoy, and reverting it must not fix the test.
    "2b-retries": {
        "cause": [("payments/config.py", "TIMEOUT_S = 10", "TIMEOUT_S = 2"),
                  ("payments/config.py", "MAX_RETRIES = 3", "MAX_RETRIES = 1")],
        "decoy": [("payments/client.py",
                   'raise ApiError(f"{method} {path}: gave up after {config.MAX_RETRIES} attempts: {last_error}")',
                   'raise ApiError(f"{method} {path}: gave up after {config.MAX_RETRIES} attempt(s); '
                   'last error ({type(last_error).__name__}): {last_error}")')],
        "test": "tests/test_client.py::test_retries_server_errors_then_succeeds",
        "expect": "stable fail",
        "frame": ("payments/client.py", "gave up after"),
        "decoy_marker": "attempt(s)",
    },
}
for sc in SCENARIOS.values():
    sc.setdefault("patch", sc.get("cause", []) + sc.get("decoy", []))


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
    ap.add_argument("--ref", default="main", help="commit/branch to check (default main); WORKTREE = checked-out files")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    py = venv_python(repo)
    snapshot = None
    if args.ref != "WORKTREE" and (repo / ".git").exists():
        # A scenario branch has the agent's changes (and its bug) checked out; plant into the clean seed instead.
        snapshot = Path(tempfile.mkdtemp(prefix="seeded-ref-"))
        archive = subprocess.run(["git", "archive", "--format=tar", args.ref], cwd=repo, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(snapshot)], input=archive, check=True)
        print(f"checking {args.ref} ({subprocess.run(['git', 'rev-parse', '--short', args.ref], cwd=repo, capture_output=True, **_ENC).stdout.strip()})")
        repo = snapshot
    try:
        return _check(repo, py, args)
    finally:
        if snapshot:
            shutil.rmtree(snapshot, ignore_errors=True)


_ENC = {"encoding": "utf-8", "errors": "replace"}


def _check(repo: Path, py: str, args) -> int:
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
        if ok and "decoy" in sc:
            # blame must be wrong: the traceback ends on the decoy edit's line, the decoy alone is
            # harmless, and the cause alone still fails
            path, line = frame.replace("\\", "/").rsplit(":", 1)
            on_decoy = sc["decoy_marker"] in (root / path).read_text(encoding="utf-8").splitlines()[int(line) - 1]
            d_root, c_root = copy_repo(repo), copy_repo(repo)
            plant(d_root, sc["decoy"])
            plant(c_root, sc["cause"])
            d_pass = sum(pytest(py, d_root, sc["test"]).returncode == 0 for _ in range(3))
            c_fail = sum(pytest(py, c_root, sc["test"]).returncode != 0 for _ in range(3))
            for r in (d_root, c_root):
                shutil.rmtree(r.parent, ignore_errors=True)
            ok = on_decoy and d_pass == 3 and c_fail == 3
            detail += (f"; that line is the decoy edit: {'yes' if on_decoy else 'NO'}; "
                       f"decoy alone passes {d_pass}/3; cause alone fails {c_fail}/3")
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
