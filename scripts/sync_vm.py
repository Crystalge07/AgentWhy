"""Copy this repo's committed HEAD to the shared VM and reinstall it there.

Works on Mac, Linux and Windows: it only needs `git`, `ssh` (built into
Windows 10/11) and your SSH key on the VM. Nothing is copied but tracked
files, so `.env`, `.venv` and reports on the VM are left alone.

One-time setup per machine (run from the repo root):

    Mac/Linux:  .venv/bin/python scripts/sync_vm.py --install-hook root@<VM-IP>
    Windows:    .venv\\Scripts\\python scripts\\sync_vm.py --install-hook root@<VM-IP>

After that every `git commit` syncs in the background (no waiting).
Results go to .git/whyagent-vm-sync.log. Sync by hand with
`... scripts/sync_vm.py`. Turn it off with `git config whyagent.vmSync false`.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

DEFAULT_DIR = "/root/hacktrent/agentwhy"
HOOK_MARK = "# whyagent: sync to VM"
# Runs under git's sh (Git for Windows ships one). Uses the venv's Python,
# never bare `python` (a Microsoft Store stub on Windows), and backgrounds
# the sync so the commit returns immediately.
HOOK_LINES = f"""
{HOOK_MARK} (scripts/sync_vm.py; disable: git config whyagent.vmSync false)
for py in .venv/bin/python .venv/Scripts/python.exe; do
  if [ -x "$py" ]; then "$py" scripts/sync_vm.py --quiet >/dev/null 2>&1 & break; fi
done
"""


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], capture_output=True, check=True,
                         encoding="utf-8", errors="replace")
    return out.stdout.strip()


def git_config(key: str) -> str | None:
    try:
        return git("config", "--get", key) or None
    except subprocess.CalledProcessError:
        return None


def log(msg: str, quiet: bool) -> None:
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    if not quiet:
        print(msg)
    try:
        with open(Path(git("rev-parse", "--git-dir")) / "whyagent-vm-sync.log",
                  "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def install_hook(host: str, remote_dir: str) -> None:
    git("config", "whyagent.vm", host)
    git("config", "whyagent.vmDir", remote_dir)
    git("config", "whyagent.vmSync", "true")
    hook = Path(git("rev-parse", "--git-path", "hooks/post-commit"))
    text = hook.read_text(encoding="utf-8") if hook.exists() else "#!/bin/sh\n"
    if HOOK_MARK not in text:
        hook.parent.mkdir(parents=True, exist_ok=True)
        with open(hook, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.rstrip("\n") + "\n" + HOOK_LINES)
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Hook installed: commits now sync to {host}:{remote_dir}")
    print("Check it: run this script once by hand, or see .git/whyagent-vm-sync.log")


def sync(host: str, remote_dir: str, quiet: bool) -> int:
    commit = git("rev-parse", "--short", "HEAD")
    d = shlex.quote(remote_dir)
    remote = (f"mkdir -p {d} && tar -x -C {d} && cd {d} && "
              f"(test -x .venv/bin/pip || python3 -m venv .venv) && "
              f".venv/bin/pip install -q -e '.[dev]'")
    archive = subprocess.Popen(["git", "archive", "--format=tar", "HEAD"],
                               stdout=subprocess.PIPE)
    ssh = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote],
        stdin=archive.stdout, capture_output=True)
    archive.stdout.close()
    archive.wait()
    if archive.returncode or ssh.returncode:
        err = ssh.stderr.decode("utf-8", errors="replace").strip()
        log(f"FAILED {commit} -> {host}: {err[-500:] or 'git archive failed'}", quiet)
        return 1
    log(f"synced {commit} -> {host}:{remote_dir}", quiet)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--install-hook", metavar="USER@HOST",
                   help="save the VM address and add the post-commit hook")
    p.add_argument("--dir", help=f"repo folder on the VM (default {DEFAULT_DIR})")
    p.add_argument("--quiet", action="store_true", help="log only (used by the hook)")
    a = p.parse_args()

    os.chdir(git("rev-parse", "--show-toplevel"))
    remote_dir = a.dir or git_config("whyagent.vmDir") or DEFAULT_DIR
    if a.install_hook:
        install_hook(a.install_hook, remote_dir)
        return 0
    if a.quiet and git_config("whyagent.vmSync") == "false":
        return 0
    host = git_config("whyagent.vm")
    if not host:
        print("No VM set. Run: scripts/sync_vm.py --install-hook root@<VM-IP>",
              file=sys.stderr)
        return 1
    return sync(host, remote_dir, a.quiet)


if __name__ == "__main__":
    sys.exit(main())
