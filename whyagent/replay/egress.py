"""Allowlisted egress for agent reruns: "network limited to the model API" (PRD, sandbox section).

The agent container joins an `--internal` Docker network, which has no route out. The only other
member is a small CONNECT proxy (run from the same HEAD image, so nothing extra to pull) that is also
on the default bridge and forwards HTTPS only to allowlisted hosts. The agent gets HTTPS_PROXY pointing
at it. Anything else (pip, curl, git clone) fails, and the attempt is logged.
"""
from __future__ import annotations

import subprocess
import time
import uuid

_ENC = {"encoding": "utf-8", "errors": "replace"}

PROXY_SRC = r'''
import asyncio, os
ALLOW = [h.strip().lower() for h in os.environ["WHYAGENT_ALLOW"].split(",") if h.strip()]

def allowed(host):
    host = host.lower()
    return any(host == a or host.endswith("." + a) for a in ALLOW)

async def pipe(r, w):
    try:
        while True:
            b = await r.read(65536)
            if not b:
                break
            w.write(b)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass

async def handle(cr, cw):
    try:
        req = (await cr.readline()).decode("latin-1").split()
        while (await cr.readline()) not in (b"\r\n", b"\n", b""):
            pass
        if len(req) < 2 or req[0] != "CONNECT":
            print("blocked", req[1] if len(req) > 1 else "?", flush=True)
            cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await cw.drain()
            return
        host, _, port = req[1].rpartition(":")
        if port != "443" or not allowed(host):
            print("blocked", req[1], flush=True)
            cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await cw.drain()
            return
        ur, uw = await asyncio.open_connection(host, 443)
        cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await cw.drain()
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    except Exception as e:
        print("error", type(e).__name__, flush=True)
    finally:
        try:
            cw.close()
        except Exception:
            pass

async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 8888)
    print("ready", flush=True)
    async with server:
        await server.serve_forever()

asyncio.run(main())
'''


class Egress:
    """Context manager: an internal network plus allowlist proxy. Use `.network` and `.env` for the agent."""

    def __init__(self, image: str, allow: list[str]):
        self.image, self.allow = image, allow
        tag = uuid.uuid4().hex[:10]
        self.network = f"whyagent-net-{tag}"
        self.proxy = f"whyagent-proxy-{tag}"
        self.blocked: list[str] = []

    @property
    def env(self) -> dict[str, str]:
        url = f"http://{self.proxy}:8888"
        return {"HTTPS_PROXY": url, "https_proxy": url, "HTTP_PROXY": url, "http_proxy": url,
                "NO_PROXY": "", "no_proxy": ""}

    def _docker(self, *a, check=True):
        p = subprocess.run(["docker", *a], capture_output=True, **_ENC)
        if check and p.returncode != 0:
            raise RuntimeError(f"docker {a[0]} failed: {p.stderr.strip()}")
        return p.stdout

    def __enter__(self):
        self._docker("network", "create", "--internal", self.network)
        try:
            self._docker("run", "-d", "--rm", "--name", self.proxy, "--memory", "256m",
                         "-e", f"WHYAGENT_ALLOW={','.join(self.allow)}", "-e", f"WHYAGENT_PROXY_SRC={PROXY_SRC}",
                         self.image, "sh", "-c", 'python -c "$WHYAGENT_PROXY_SRC"')
            self._docker("network", "connect", self.network, self.proxy)
            for _ in range(50):
                if "ready" in self._docker("logs", self.proxy, check=False):
                    return self
                time.sleep(0.1)
            raise RuntimeError("egress proxy didn't start: " + self._docker("logs", self.proxy, check=False)[-300:])
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        logs = self._docker("logs", self.proxy, check=False)
        self.blocked = sorted({ln.split()[1] for ln in logs.splitlines() if ln.startswith("blocked ")})
        self._docker("rm", "-f", self.proxy, check=False)
        self._docker("network", "rm", self.network, check=False)
        return False
