"""Where a test command actually executes: on the host, or in a throwaway Docker container.

PRD "Sandbox and execution environment": one image per analysis, built at HEAD so its dependencies
cover the final code; each run mounts the state's worktree at /work, with the network off and a
timeout, and the container is deleted afterwards.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

_ENC = {"encoding": "utf-8", "errors": "replace"}


class SandboxError(RuntimeError):
    """The sandbox itself failed (Docker missing, image build failed). Not a test outcome."""


@dataclass
class Exec:
    exit_code: int
    output: str
    timed_out: bool = False


class LocalSandbox:
    """Runs the test directly on the host, in the worktree. No isolation; for dev and unit tests."""
    name = "local"

    def prepare(self):
        pass

    def exec(self, workdir: str, cmd: str, timeout: int, env: dict | None = None, **_ignored) -> Exec:
        home = tempfile.mkdtemp(prefix="whyagent-home-") if env else None
        full_env = {**os.environ, **({"HOME": home} if home else {}), **(env or {})}
        try:
            p = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True, timeout=timeout, env=full_env, **_ENC)
        except subprocess.TimeoutExpired:
            return Exec(-1, "", timed_out=True)
        finally:
            if home:
                shutil.rmtree(home, ignore_errors=True)
        return Exec(p.returncode, p.stdout + p.stderr)


def docker_available() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker not found on PATH"
    p = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, **_ENC)
    if p.returncode != 0:
        return False, "Docker daemon isn't running (start Docker Desktop)"
    return True, f"Docker {p.stdout.strip()}"


def _default_setup(files: set[str]) -> str:
    cmds = ["pip install --no-cache-dir pytest"]
    if "requirements.txt" in files:
        cmds.append("pip install --no-cache-dir -r requirements.txt")
    if "pyproject.toml" in files or "setup.py" in files:
        cmds.append("pip install --no-cache-dir -e .")
    return " && ".join(cmds)


class DockerSandbox:
    """One image per (repo, HEAD, setup); one `docker run --rm` per test run."""
    name = "docker"

    def __init__(self, repo: str, head: str, setup: str | None = None, network: str = "none",
                 memory: str = "2g", base_image: str | None = None, quiet: bool = False):
        self.repo = os.path.abspath(repo)
        self.head = head
        self.setup = setup
        self.network = network
        self.memory = memory
        self.base_image = base_image or f"python:{sys.version_info.major}.{sys.version_info.minor}-slim"
        self.quiet = quiet
        self.image: str | None = None

    def _git(self, *a) -> str:
        return subprocess.run(["git", *a], cwd=self.repo, capture_output=True, check=True, **_ENC).stdout

    def _tag(self, files: set[str]) -> str:
        has_df = "Dockerfile" in files
        key = "|".join([self.head, self.setup or "", self.base_image,
                        self._git("show", f"{self.head}:Dockerfile") if has_df else ""])
        name = re.sub(r"[^a-z0-9]+", "-", Path(self.repo).name.lower()).strip("-") or "repo"
        return f"whyagent-{name}:{self.head[:12]}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"

    def prepare(self):
        """Build the HEAD image once; reuse it if this exact image already exists."""
        ok, why = docker_available()
        if not ok:
            raise SandboxError(f"{why}. Use --sandbox local to run tests on the host instead.")
        files = set(self._git("ls-tree", "--name-only", self.head).split())
        tag = self._tag(files)
        if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
            self.image = tag
            return
        ctx = tempfile.mkdtemp(prefix="whyagent-img-")
        try:
            # Clean snapshot of HEAD: no .venv, .git or local junk in the build context.
            archive = subprocess.run(["git", "archive", "--format=tar", self.head], cwd=self.repo,
                                     capture_output=True, check=True).stdout
            subprocess.run(["tar", "-x", "-C", ctx], input=archive, check=True)
            if "Dockerfile" not in files:
                Path(ctx, "Dockerfile").write_text(
                    f"FROM {self.base_image}\nWORKDIR /work\nCOPY . /work\n"
                    f"RUN {self.setup or _default_setup(files)}\n", encoding="utf-8")
            elif self.setup:
                df = Path(ctx, "Dockerfile")
                df.write_text(df.read_text(encoding="utf-8") + f"\nRUN {self.setup}\n", encoding="utf-8")
            if not self.quiet:
                print(f"sandbox: building image {tag} (once per HEAD)…", file=sys.stderr)
            p = subprocess.run(["docker", "build", "-t", tag, ctx], capture_output=True, **_ENC)
            if p.returncode != 0:
                tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-25:])
                raise SandboxError(f"docker build failed:\n{tail}")
        finally:
            shutil.rmtree(ctx, ignore_errors=True)
        self.image = tag

    def extend(self, label: str, dockerfile_lines: list[str]) -> str:
        """Build (once) an image FROM the HEAD test image with extra lines, e.g. an agent CLI. Returns its tag."""
        if self.image is None:
            self.prepare()
        body = "\n".join(dockerfile_lines)
        tag = f"{self.image}-{label}-{hashlib.sha1(body.encode()).hexdigest()[:8]}"
        if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
            return tag
        if not self.quiet:
            print(f"sandbox: building image {tag}…", file=sys.stderr)
        p = subprocess.run(["docker", "build", "-t", tag, "-"], input=f"FROM {self.image}\n{body}\n",
                           capture_output=True, **_ENC)
        if p.returncode != 0:
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-25:])
            raise SandboxError(f"docker build ({label}) failed:\n{tail}")
        return tag

    def exec(self, workdir: str, cmd: str, timeout: int, env: dict | None = None, network: str | None = None,
             image: str | None = None, readonly: tuple[str, ...] = ()) -> Exec:
        """Run cmd in a fresh container with workdir at /work. `env` values are passed by name, never on argv,
        so API keys don't show up in `ps`. `readonly` paths (relative to workdir) are mounted read-only."""
        if self.image is None:
            self.prepare()
        name = f"whyagent-run-{uuid.uuid4().hex[:12]}"
        wd = os.path.abspath(workdir)
        args = ["docker", "run", "--rm", "--name", name, "--network", network or self.network, "--memory", self.memory,
                "-v", f"{wd}:/work", "-w", "/work",
                "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-e", "PYTEST_ADDOPTS=-p no:cacheprovider"]
        for rel in readonly:
            args += ["-v", f"{os.path.join(wd, rel)}:/work/{rel.replace(os.sep, '/')}:ro"]
        for k in (env or {}):
            args += ["-e", k]
        if hasattr(os, "getuid"):
            # Files the test writes stay owned by us, so worktree teardown can delete them (matters on Linux/Vultr).
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        args += [image or self.image, "sh", "-c", cmd]
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout, env={**os.environ, **(env or {})}, **_ENC)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            return Exec(-1, "", timed_out=True)
        if p.returncode == 125:  # docker run itself failed, the test never started
            raise SandboxError(f"docker run failed: {p.stderr.strip()}")
        return Exec(p.returncode, p.stdout + p.stderr)
