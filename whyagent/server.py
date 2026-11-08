"""`whyagent --serve`: the results site. Localhost only, standard library only.

  /             every analysis in .whyagent/history (and older .whyagent/<name>/report.json folders), newest first
  /a/<id>       its report once finished; while it runs, a live view fed by its events.jsonl (see events.py)
  /api/...      the list, events since a byte offset, and POST to start a new analysis

An analysis started from the terminal writes the same history folder, so it shows up here live as well.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STALE_S = 15 * 60  # no event for this long and no report: the process died (Ctrl-C, killed)
_ID = re.compile(r"^[\w.-]{1,64}$")


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _done_event(p: Path) -> dict | None:
    """The analysis's `done` event, if it has finished (anywhere in the file: output can follow it)."""
    try:
        for ln in reversed(p.read_text(encoding="utf-8", errors="replace").splitlines()):
            if '"type": "done"' in ln:
                return json.loads(ln)
    except (OSError, ValueError):
        pass
    return None


def _last_event(p: Path) -> dict | None:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError, IndexError):
        return None


class Results:
    def __init__(self, history: Path):
        self.history = history
        self.base = history.parent  # .whyagent: older runs kept their own folders here (s1/, s2b/, ...)

    def folder(self, aid: str) -> Path | None:
        if not _ID.match(aid):
            return None
        for d in (self.history / aid, self.base / aid):
            if d.is_dir() and d.name != "history":
                return d
        return None

    def summary(self, d: Path) -> dict | None:
        meta = _read_json(d / "meta.json") or {}
        rep = _read_json(d / "report.json")
        if not meta and not rep:
            return None
        m = (rep or {}).get("meta") or {}
        last, done = _last_event(d / "events.jsonl"), _done_event(d / "events.jsonl")
        if rep:
            status = "done" if not last or done else "running"
        elif done or (last and time.time() - last.get("t", 0) > STALE_S):
            status = "stopped"
        else:
            status = "running" if last else "starting"
        test = m.get("test") or meta.get("test") or ""
        node = next((w for w in test.split() if "::" in w), test)
        out = {"id": d.name, "status": status, "test": node.split("::")[-1] or node, "testId": node,
               "repo": m.get("repo") or meta.get("repo") or "",
               "when": m.get("generated_at") or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(meta.get("started", d.stat().st_mtime))),
               "code": done.get("code") if done else None}
        if rep:
            rc, ses = rep.get("root_cause"), rep.get("session") or {}
            step = next((s for s in ses.get("steps") or [] if rc and s["id"] == rc["step_id"]), None)
            out.update(cause={"edit": step["id"], "file": step["file"], "line": step.get("line")} if step else None,
                       mode=(rep.get("precheck") or {}).get("mode"),
                       runs=sum(1 for r in rep.get("runs") or [] if not r.get("cached")),
                       why=None if step else _why(rep))
        return out

    def list(self) -> list[dict]:
        dirs = [d for d in self.history.glob("*") if d.is_dir()] if self.history.is_dir() else []
        dirs += [d for d in self.base.glob("*") if d.is_dir() and d.name != "history" and (d / "report.json").exists()]
        rows = [s for s in (self.summary(d) for d in dirs) if s]
        return sorted(rows, key=lambda r: r["when"] or "", reverse=True)

    def events(self, aid: str, offset: int) -> dict:
        d = self.folder(aid)
        if d is None:
            return {"events": [], "next": offset, "status": "missing"}
        p, evs = d / "events.jsonl", []
        try:
            with open(p, "rb") as f:
                f.seek(offset)
                chunk = f.read(512 * 1024)
            end = chunk.rfind(b"\n") + 1  # only whole lines; a half-written one waits for the next poll
            for ln in chunk[:end].decode("utf-8", "replace").splitlines():
                try:
                    evs.append(json.loads(ln))
                except ValueError:
                    pass
            offset += end
        except OSError:
            pass
        s = self.summary(d) or {"status": "starting"}
        return {"events": evs, "next": offset, "status": s["status"]}


def _why(rep: dict) -> str:
    notes = rep.get("notes") or []
    stop = next((n for n in notes if n.startswith(("Aborted", "Analysis stopped", "HEAD passes", "Bisect inconclusive",
                                                   "Every commit", "Couldn't load"))), None)
    if stop:
        return stop
    cm = rep.get("commit")
    if cm and cm.get("human"):
        return f"Commit {cm['sha'][:7]} has no Entire checkpoint: a person wrote it."
    return "No root cause found."


def _spawn(results: Results, body: dict) -> str:
    test, good = (body.get("test") or "").strip(), (body.get("good") or "").strip()
    if not test or not good:
        raise ValueError("test and good commit are required")
    aid = uuid.uuid4().hex[:12]
    folder = results.history / aid
    folder.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "whyagent.cli", "--test", test, "--good", good, "--id", aid,
           "--report", str(results.base / "report.html"), "--sandbox", body.get("sandbox") or "docker",
           "--workers", str(int(body.get("workers") or 2))]
    if (body.get("line") or "").strip():
        cmd += ["--line", body["line"].strip()]
    if body.get("no_rerun"):
        cmd.append("--no-rerun")
    log = open(folder / "stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})

    def wait():
        code = proc.wait()
        log.close()
        if not _done_event(folder / "events.jsonl"):  # died before it could say so (bad args, import error)
            tail = (folder / "stdout.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:]
            with open(folder / "events.jsonl", "a", encoding="utf-8") as f:
                for ln in tail:
                    f.write(json.dumps({"t": time.time(), "type": "line", "text": ln}) + "\n")
                f.write(json.dumps({"t": time.time(), "type": "done", "code": code}) + "\n")
            if not (folder / "meta.json").exists():
                (folder / "meta.json").write_text(json.dumps({"test": test, "started": time.time()}), encoding="utf-8")

    threading.Thread(target=wait, daemon=True).start()
    return aid


def _back_link(html: str) -> str:
    """The saved report, with a way back to the list."""
    return html.replace('<div class="logo">whyagent <span>report</span></div>',
                        '<a class="logo" href="/" style="color:inherit;text-decoration:none">whyagent <span>results</span></a>'
                        '<a class="tab" href="/" style="display:flex;align-items:center;text-decoration:none">All results</a>', 1)


def serve(history: Path, port: int = 8700, open_browser: bool = False) -> int:
    results = Results(history)
    app = resources.files("whyagent").joinpath("app.html").read_text(encoding="utf-8")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet: the live view polls
            pass

        def send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def json(self, obj, code: int = 200):
            self.send(code, json.dumps(obj), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            if not parts:
                return self.send(200, app)
            if parts == ["api", "analyses"]:
                return self.json(results.list())
            if len(parts) == 3 and parts[:2] == ["api", "events"]:
                off = int((parse_qs(u.query).get("from") or ["0"])[0] or 0)
                return self.json(results.events(parts[2], off))
            if len(parts) == 2 and parts[0] == "a":
                d = results.folder(parts[1])
                if d is None:
                    return self.send(404, "No such analysis.", "text/plain; charset=utf-8")
                s = results.summary(d) or {}
                if s.get("status") == "done" and (d / "report.html").exists():
                    return self.send(200, _back_link((d / "report.html").read_text(encoding="utf-8")))
                return self.send(200, app)  # live view (or a stopped run's log)
            self.send(404, "Not found.", "text/plain; charset=utf-8")

        def do_POST(self):
            if urlparse(self.path).path != "/api/analyses":
                return self.send(404, "Not found.", "text/plain; charset=utf-8")
            # Starting an analysis runs a command: only from this site's own page. A cross-site form can't send
            # application/json without a CORS preflight (which we never answer), and the Origin must be ours.
            origin = self.headers.get("Origin")
            if (self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"
                    or (origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"))):
                return self.json({"error": "forbidden"}, 403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                return self.json({"id": _spawn(results, body)})
            except (ValueError, OSError) as e:
                return self.json({"error": str(e)}, 400)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/"
    print(f"whyagent results: {url}  (history: {history})  Ctrl-C to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
