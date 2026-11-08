"""Demo preflight: checks infra, the seeded repo, the recorded sessions and the pipeline in one go.

    python demo/preflight.py [../payments-api-client] [--quick] [--seeded] [--live-gemini]

Every check prints PASS / FAIL / SKIP / WARN. SKIP means blocked on access we don't have yet
(Vultr, Gemini) or a check you didn't ask for; it never fails the run. Exit code 1 if any FAIL.

  --quick        skip test runs (tools, config, Entire and session parsing only, ~10 s)
  --seeded       also run demo/check_seeded.py on a clean snapshot of payments-api-client main (~60 s)
  --live-gemini  actually call Gemini once (costs quota); otherwise Gemini checks are SKIP

Read-only for both repos: test runs use throwaway git worktrees and containers, never the checkout.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_ENC = {"encoding": "utf-8", "errors": "replace"}

PY = "python -m pytest -q -p no:cacheprovider"
# branch, --good, target test, flaky?
SCENARIOS = [
    ("scenario-1", "87dc38e", "tests/test_client.py::test_list_invoices_returns_every_invoice", False),
    ("scenario-2b-clean", "f39623e", "tests/test_client.py::test_retries_server_errors_then_succeeds", False),
    ("scenario-3-haiku", "87dc38e", "tests/test_settlement.py::test_invoice_paid_after_charge_settles", True),
]
SECRET_RE = re.compile(r"AIza[0-9A-Za-z_\-]{30,}|AQ\.[0-9A-Za-z_\-]{30,}|sk-ant-[0-9A-Za-z_\-]{20,}"
                       r"|postgres(ql)?://[^:\s/<]+:[^@\s<]+@")  # `<pw>` placeholders in docs aren't secrets

results: list[tuple[str, str, str, str]] = []


def mark(area: str, name: str, status: str, detail: str = ""):
    results.append((area, name, status, detail))
    print(f"{status:<5} {area:<8} {name:<34} {detail}", flush=True)


def check(area: str, name: str, fn, skip_on: tuple = ()):
    """Run fn() -> (status, detail) | detail. Exceptions are FAIL (or SKIP for skip_on types)."""
    try:
        r = fn()
        status, detail = r if isinstance(r, tuple) else ("PASS", r)
    except skip_on as e:  # noqa: B030
        status, detail = "SKIP", str(e)
    except Exception as e:  # noqa: BLE001
        status, detail = "FAIL", f"{type(e).__name__}: {str(e).strip().splitlines()[0][:160] if str(e).strip() else ''}"
    mark(area, name, status, str(detail)[:200])
    return status


def sh(args, cwd=None, timeout=120, check=True) -> str:
    p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, **_ENC)
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or f"exit {p.returncode}")
    return p.stdout


# ------------------------------------------------------------------ infra

def infra(demo: Path):
    check("infra", "python >= 3.10", lambda: ("PASS" if sys.version_info >= (3, 10) else "FAIL", sys.version.split()[0]))

    def deps():
        import dotenv, psycopg, pytest  # noqa: F401,E401
        import whyagent.cli  # noqa: F401
        return "whyagent, psycopg, python-dotenv, pytest import"
    check("infra", "python deps", deps)
    check("infra", "git", lambda: sh(["git", "--version"]).strip())

    def docker():
        from whyagent.replay import docker_available
        ok, why = docker_available()
        return ("PASS" if ok else "FAIL", why)
    check("infra", "docker daemon", docker)
    check("infra", "entire CLI", lambda: sh(["entire", "--version"]).splitlines()[0])

    def entire_status():
        out = sh(["entire", "status"], cwd=demo)
        if "Enabled" not in out:
            return "FAIL", out.strip().splitlines()[0]
        warn = "not yet on origin" in out
        return ("WARN" if warn else "PASS"), out.strip().splitlines()[0] + (" (unpushed checkpoints)" if warn else "")
    check("infra", "entire enabled in demo repo", entire_status)

    def hooks():
        cfg = demo / ".claude" / "settings.json"
        return ("PASS", "Claude Code hooks installed") if cfg.exists() and "entire hooks" in cfg.read_text(**_ENC) \
            else ("FAIL", f"no entire hooks in {cfg}; run `entire enable --agent claude-code`")
    check("infra", "entire hooks (demo repo)", hooks)

    def stale():
        wt = [ln for ln in sh(["git", "worktree", "list", "--porcelain"], cwd=demo).splitlines()
              if ln.startswith("worktree ") and "whyagent-" in ln]
        dk = sh(["docker", "ps", "-aq", "--filter", "name=whyagent-"], check=False).split() if shutil.which("docker") else []
        nets = sh(["docker", "network", "ls", "-q", "--filter", "name=whyagent-net-"], check=False).split() \
            if shutil.which("docker") else []
        if wt or dk or nets:
            return "WARN", (f"{len(wt)} worktree(s) (git worktree prune), {len(dk)} container(s), "
                            f"{len(nets)} network(s) left by an interrupted run")
        return "no leftover worktrees, containers or networks"
    check("infra", "no leftovers from killed runs", stale)


# ------------------------------------------------------------------ config

def config(demo: Path):
    def env_file():
        if not (ROOT / ".env").exists():
            return "WARN", ".env missing (cp .env.example .env)"
        tracked = sh(["git", "ls-files", ".env"], cwd=ROOT).strip()
        ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode == 0
        return ("FAIL", ".env is tracked by git") if tracked else \
               ("PASS", ".env present and gitignored") if ignored else ("FAIL", ".env not gitignored")
    check("config", ".env present + gitignored", env_file)

    def secrets():
        hits = []
        for repo in (ROOT, demo):
            for f in sh(["git", "ls-files"], cwd=repo).splitlines():
                p = repo / f
                if p.is_file() and p.stat().st_size < 2_000_000:
                    if SECRET_RE.search(p.read_text(**_ENC)):
                        hits.append(f"{repo.name}/{f}")
        return ("FAIL", "secret-looking strings in " + ", ".join(hits[:5])) if hits else "no keys or DB passwords in tracked files"
    check("config", "no secrets in tracked files", secrets)

    def whyagent_ignored():
        ok = subprocess.run(["git", "check-ignore", "-q", ".whyagent/report.html"], cwd=demo).returncode == 0
        return ("PASS", ".whyagent/ ignored") if ok else ("WARN", ".whyagent/ not in payments-api-client .gitignore; reports show as untracked")
    check("config", "demo repo ignores .whyagent/", whyagent_ignored)

    def tiger():
        url = os.environ.get("TIGER_DATABASE_URL")
        if not url:
            return "WARN", "TIGER_DATABASE_URL unset: run log, chart and shared Brain are off (analysis still works)"
        from whyagent.tiger import connect
        with connect(url) as c:
            c.execute("SELECT 1")
        return "connects"
    check("config", "Tiger Data", tiger)


# ------------------------------------------------------------------ blocked integrations

def blocked(live_gemini: bool):
    if not os.environ.get("GEMINI_API_KEY"):
        mark("gemini", "lesson (Stage 7)", "SKIP", "GEMINI_API_KEY unset; use --lesson TEXT")
        mark("gemini", "gemini-cli rerun agent (Stage 8)", "SKIP", "GEMINI_API_KEY unset; use --rerun-agent claude-code")
    elif not live_gemini:
        mark("gemini", "lesson (Stage 7)", "SKIP", "key set; pass --live-gemini to spend one call checking it")
        mark("gemini", "gemini-cli rerun agent (Stage 8)", "SKIP", "not exercised by preflight")
    else:
        def ping():
            from whyagent.rerun.lesson import gemini_generate
            return "replied: " + gemini_generate("Reply with the single word OK.").strip()[:40]
        check("gemini", "lesson (Stage 7)", ping)
        mark("gemini", "gemini-cli rerun agent (Stage 8)", "SKIP", "not exercised by preflight")
    if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")):
        mark("rerun", "claude-code rerun agent auth", "PASS", "key/token set (not exercised)")
    else:
        mark("rerun", "claude-code rerun agent auth", "SKIP", "no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN")
    mark("vultr", "sandbox VM", "SKIP", "no code touchpoint; whyagent just runs on the VM (docs/setup.md)")


# ------------------------------------------------------------------ demo repo + logic

def scenarios(demo: Path, quick: bool):
    from whyagent import entire
    from whyagent.adapters import load_replayable
    from whyagent.replay import DockerSandbox, Replayer, docker_available
    from whyagent.types import PASS, State

    docker_ok = docker_available()[0]
    for branch, good, test, flaky in SCENARIOS:
        area = branch.replace("scenario-", "sc-")
        try:
            head = sh(["git", "rev-parse", branch], cwd=demo).strip()
            good_sha = sh(["git", "rev-parse", good], cwd=demo).strip()
        except Exception as e:  # noqa: BLE001
            mark(area, "branch + good commit exist", "FAIL", str(e)[:160])
            continue
        mark(area, "branch + good commit exist", "PASS", f"{head[:7]} / {good_sha[:7]}")
        commits = sh(["git", "rev-list", f"{good_sha}..{head}"], cwd=demo).split()
        agent = [c for c in commits if entire.checkpoint_id(c, cwd=str(demo))]
        if not agent:
            mark(area, "Entire checkpoint trailer", "FAIL", "no commit since --good carries Entire-Checkpoint")
            continue
        mark(area, "Entire checkpoint trailer", "PASS", f"{len(agent)} agent commit(s), e.g. {agent[0][:7]}")

        def steps(c=agent[0]):
            st, fid = load_replayable(c, cwd=str(demo))
            if not st:
                return "FAIL", "session parsed to 0 steps"
            sess = {s.session_id for s in st if s.session_id}
            note = f"{len(st)} steps, {len(sess)} session(s), shell={len(fid.shell)}, unattributed={len(fid.unattributed)}"
            return ("PASS" if not fid.unattributed and not fid.dropped else "WARN"), note
        check(area, "session -> ordered steps (replays G)", steps)

        if quick or not docker_ok:
            mark(area, "good passes / HEAD fails", "SKIP", "--quick" if quick else "docker unavailable")
            continue

        def good_bad():
            sb = DockerSandbox(str(demo), head, quiet=True)
            rp = Replayer(str(demo), [], f"{PY} {test}", sandbox=sb, workers=2)
            g, h = rp.run_many([State(good_sha, [], head), State(head, [], head)], 1)
            if g[0].outcome != PASS:
                return "FAIL", f"good commit fails (exit {g[0].exit_code})"
            hs = rp.run(State(head, [], head), 10 if flaky else 1, until_fail=True)
            fails = sum(r.outcome != PASS for r in hs)
            if not fails:
                return "FAIL", f"HEAD passes {len(hs)}/{len(hs)}: nothing for bisect to find"
            if any(r.exit_code in (4, 5) for r in hs):
                return "FAIL", "pytest exit 4/5: test id not found or nothing collected"
            return f"good passes; HEAD fails ({fails}/{len(hs)} runs{', flaky' if flaky else ''})"
        check(area, "good passes / HEAD fails", good_bad)


def pipeline_e2e(demo: Path, quick: bool):
    """The real CLI on scenario 2b: stages 0-6 (~15 s), then Stage 8 with the stub agent (no API, ~20 s)."""
    if quick:
        mark("e2e", "stages 0-6 on scenario-2b-clean", "SKIP", "--quick")
        mark("e2e", "stage 8 via --rerun-agent stub", "SKIP", "--quick")
        return
    branch, good, test, _ = SCENARIOS[1]
    out_dir = Path(tempfile.mkdtemp(prefix="whyagent-preflight-"))
    wt = out_dir / "demo"
    # arm A (no lesson) repeats the agent's global config change; arm B (lesson in its prompt) leaves config alone
    stub = ('case "$WHYAGENT_PROMPT" in *Lesson*) true ;; *) sed -i "s/^TIMEOUT_S = 10$/TIMEOUT_S = 2/; '
            's/^MAX_RETRIES = 3$/MAX_RETRIES = 1/" payments/config.py ;; esac')
    try:
        sh(["git", "worktree", "add", "--detach", str(wt), branch], cwd=demo)
        env = {**os.environ, "TIGER_DATABASE_URL": "", "WHYAGENT_BRAIN": str(out_dir / "brain.jsonl"),
               "WHYAGENT_STUB_CMD": stub}
        t = time.time()
        p = subprocess.run([sys.executable, "-m", "whyagent.cli", "--good", good, "--test", f"{PY} {test}",
                            "--lesson", "preflight", "--rerun-agent", "stub", "--report", str(out_dir / "report.html")],
                           cwd=wt, env=env, capture_output=True, timeout=900, **_ENC)
        out = p.stdout + p.stderr
        last = (out.strip().splitlines() or ["(no output)"])[-1]

        def stages():
            m = re.search(r"root cause: edit #(\d+) (\S+)", out)
            if not m:
                return "FAIL", f"exit {p.returncode}: {last}"
            if not m.group(2).startswith("payments/config.py"):
                return "FAIL", f"blamed {m.group(2)}, expected payments/config.py"
            runs = re.search(r"test runs used: (\d+)", out)
            return f"edit #{m.group(1)} {m.group(2)} in {runs.group(1) if runs else '?'} test runs"
        check("e2e", "stages 0-6 on scenario-2b-clean", stages)

        def reruns():
            m = re.search(r"rerun: without lesson .*", out)
            if not m:
                return "FAIL", f"exit {p.returncode}: {last}"
            return ("PASS" if "VALIDATED" in m.group(0) and p.returncode == 0 else "FAIL"), \
                m.group(0).replace("rerun: ", "") + f" ({time.time() - t:.0f}s total)"
        check("e2e", "stage 8 via --rerun-agent stub", reruns)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=demo, capture_output=True)
        shutil.rmtree(out_dir, ignore_errors=True)


def seeded(demo: Path, run: bool):
    if not run:
        mark("seeded", "check_seeded on payments-api-client main", "SKIP", "pass --seeded (~60 s)")
        return

    def go():
        p = subprocess.run([sys.executable, str(ROOT / "demo" / "check_seeded.py"), str(demo), "--ref", "main",
                            "--flaky-runs", "20"], capture_output=True, timeout=900, **_ENC)
        bad = [ln for ln in p.stdout.splitlines() if ln.startswith("✗")] or p.stderr.strip().splitlines()[-1:]
        return ("PASS", "baseline + 4 planted bugs behave") if p.returncode == 0 else ("FAIL", "; ".join(bad)[:200])
    check("seeded", "check_seeded on payments-api-client main", go)


def unit_tests():
    def go():
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"], cwd=ROOT, capture_output=True, timeout=600, **_ENC)
        last = (p.stdout.strip().splitlines() or ["?"])[-1]
        return ("PASS" if p.returncode == 0 else "FAIL"), last
    check("tests", "whyagent unit tests", go)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("demo", nargs="?", default=str(ROOT.parent / "payments-api-client"))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--seeded", action="store_true")
    ap.add_argument("--live-gemini", action="store_true")
    a = ap.parse_args()
    demo = Path(a.demo).resolve()
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    if not (demo / ".git").exists():
        print(f"FAIL  demo repo not found at {demo}")
        return 1
    infra(demo)
    config(demo)
    blocked(a.live_gemini)
    scenarios(demo, a.quick)
    pipeline_e2e(demo, a.quick)
    seeded(demo, a.seeded)
    if not a.quick:
        unit_tests()
    counts = {k: sum(r[2] == k for r in results) for k in ("PASS", "WARN", "SKIP", "FAIL")}
    print("\n" + " · ".join(f"{v} {k}" for k, v in counts.items()))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
