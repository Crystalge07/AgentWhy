"""`whyagent --serve`: the results site. Localhost only, standard library only.

  /             every analysis in .whyagent/history (and older .whyagent/<name>/report.json folders), newest first
  /a/<id>       its report once finished; while it runs, a live view fed by its events.jsonl (see events.py)
  /live[/<id>]  the pipeline view: one analysis drawn stage by stage (the newest one without an id).
                Live from /api/stream/<id> (Server-Sent Events), or ?replay=1 from /api/replay/<id>: same page,
                only the event source differs, and it says which one it is showing.
  /tiger, /vultr  how we use Tiger Data and Vultr, from live Tiger queries (dashboards.py)
  /entire, /gemini  how we use Entire and Gemini, with what they gave the newest finished analysis (/api/latest)
  /site.css     the shared look; every page's top bar is filled in by _page (Pipeline, Results, "Built with" menu)
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
    elif body.get("rerun_agent") in ("claude-code", "gemini-cli"):
        cmd += ["--rerun-agent", body["rerun_agent"]]
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


def stream(results: Results, aid: str, write, poll: float = 0.25, idle_s: float = STALE_S) -> None:
    """Server-Sent Events for one analysis: every event so far, then new ones as the run writes them, until its
    `done` event (or nothing new for idle_s). `write` sends bytes and raises OSError when the browser leaves."""
    offset, last = 0, time.time()
    while True:
        out = results.events(aid, offset)
        offset = out["next"]
        for ev in out["events"]:
            write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
            last = time.time()
            if ev.get("type") == "done":
                return
        if out["status"] in ("missing", "stopped", "done") and not out["events"]:
            write(f"event: end\ndata: {json.dumps({'status': out['status']})}\n\n".encode("utf-8"))
            return
        if time.time() - last > idle_s:
            return
        write(b": keep-alive\n\n")
        time.sleep(poll)


def _dashboard(name: str) -> dict:
    """/api/tiger and /api/vultr: live Tiger queries, or {"error": ...} the page shows."""
    import importlib

    from . import dashboards, tiger
    importlib.reload(dashboards)  # like the pages: an edit shows on reload, no server restart mid-demo
    try:
        conn = tiger.connect()
    except Exception as e:  # noqa: BLE001
        return {"error": f"Couldn't connect to Tiger Data ({type(e).__name__})."}
    if conn is None:
        return {"error": "TIGER_DATABASE_URL isn't set, so there's nothing to show."}
    try:
        with conn:
            return json.loads(json.dumps(getattr(dashboards, name)(conn), default=str))
    except Exception as e:  # noqa: BLE001
        return {"error": f"Tiger query failed: {type(e).__name__}: {str(e)[:200]}"}


SPONSORS = [  # (path, name, what it does for whyagent): the top bar's "Built with" menu
    ("entire", "Entire", "The agent's recorded session: every edit and why"),
    ("gemini", "Gemini", "Writes the lesson from the agent's reasoning"),
    ("tiger", "Tiger Data", "Memory: every test run and proven lesson"),
    ("vultr", "Vultr", "Runs the sandboxes in parallel"),
]
_CHEVRON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>'
# Light/dark: the saved choice is applied in <head> before first paint; with none saved the system theme wins.
THEME_HEAD = ("<script>try{const t=localStorage.getItem('agentwhy-theme');"
              "if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>")
THEME_TOGGLE = (  # bottom-right corner of every page: shows the theme you'd switch to
    '<button type="button" class="theme-fab" aria-label="Switch light or dark theme" title="Light or dark">'
    '<span class="moon">🌙</span><span class="sun">☀️</span></button>'
    "<script>document.currentScript.previousElementSibling.onclick=()=>{const r=document.documentElement,"
    "d=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches,t=d?'light':'dark';"
    "r.dataset.theme=t;try{localStorage.setItem('agentwhy-theme',t)}catch(e){}}</script>")


def site_bar(current: str, extra: str = "") -> str:
    """The top bar every page shares, so moving between pages never shifts it. `current`: live, results or a sponsor.
    `extra` goes between the logo and the links (the pipeline view's title and replay controls)."""
    cur = lambda k: ' aria-current="page"' if k == current else ""  # noqa: E731
    items = "".join(f'<a href="/{k}"{cur(k)}><b>{n}</b><span>{d}</span></a>' for k, n, d in SPONSORS)
    menu_cur = " current" if current in {k for k, _, _ in SPONSORS} else ""
    return (f'<header class="site-bar"><a class="site-logo" href="/live">agent<i>why</i></a>{extra}<span class="site-sp"></span>'
            f'<nav class="site-nav" aria-label="Site"><a href="/live"{cur("live")}>Pipeline</a><a href="/"{cur("results")}>Results</a>'
            f'<div class="site-menu{menu_cur}"><button type="button" aria-expanded="false" aria-haspopup="true">Built with{_CHEVRON}</button>'
            f'<div class="site-menu-list" hidden>{items}</div></div></nav></header>'
            "<script>(()=>{const m=document.currentScript.previousElementSibling.querySelector('.site-menu'),b=m.querySelector('button'),"
            "l=m.querySelector('.site-menu-list'),set=o=>{l.hidden=!o;b.setAttribute('aria-expanded',o)};"
            "b.onclick=e=>{e.stopPropagation();set(l.hidden)};document.addEventListener('click',e=>{if(!m.contains(e.target))set(false)});"
            "document.addEventListener('keydown',e=>{if(e.key==='Escape')set(false)})})()</script>" + THEME_TOGGLE)


def _page(name: str, current: str) -> str:
    """A page of the site, read per request (an edit shows on reload, no server restart mid-demo), with its top bar."""
    html = resources.files("whyagent").joinpath(name).read_text(encoding="utf-8")
    html = html.replace("</head>", THEME_HEAD + "</head>", 1)
    m = re.search(r"<!--site-bar(?::(.*?))?-->", html, re.S)
    return html if not m else html[:m.start()] + site_bar(current, m.group(1) or "") + html[m.end():]


def _back_link(html: str) -> str:
    """The saved report (a standalone file), given the site's top bar and stylesheet."""
    html = html.replace("</head>", '<link rel="stylesheet" href="/site.css"></head>', 1)
    html = re.sub(r"<!--theme-fab-->.*?<!--/theme-fab-->", "", html, count=1, flags=re.S)
    return re.sub(r'<nav class="bar" aria-label="Report navigation">\s*<div class="logo">(?:agentwhy|whyagent) <span>report</span></div>',
                  lambda _: site_bar("results") + '<nav class="bar sub-bar" aria-label="Report navigation">', html, count=1)


def latest(results: "Results") -> dict:
    """/api/latest: what Entire and Gemini contributed to the newest finished analysis that found a cause."""
    for row in results.list():
        if row["status"] != "done" or not row.get("cause"):
            continue
        d = results.folder(row["id"])
        rep = _read_json(d / "report.json") if d else None
        if not rep:
            continue
        ses, rc, cm = rep.get("session") or {}, rep.get("root_cause") or {}, rep.get("commit") or {}
        steps = ses.get("steps") or []
        step = next((s for s in steps if s["id"] == rc.get("step_id")), {})
        cut = lambda t, n=600: (t or "")[:n]  # noqa: E731
        return {"id": row["id"], "repo": (rep.get("meta") or {}).get("repo"), "test": row["test"],
                "checkpoint": cm.get("checkpoint"), "commit": (cm.get("sha") or "")[:7], "agent": ses.get("agent"),
                "prompt": cut(ses.get("prompt"), 800), "edits": len(steps),
                "with_reasoning": sum(1 for s in steps if s.get("reasoning")), "with_trigger": sum(1 for s in steps if s.get("trigger")),
                "culprit": {"edit": step.get("id"), "file": step.get("file"), "line": step.get("line"),
                            "reasoning": cut(step.get("reasoning")), "trigger": cut(step.get("trigger")),
                            "old": cut(step.get("old_text"), 400), "new": cut(step.get("new_text"), 400)},
                "lesson": rep.get("lesson"), "rerun": {k: v for k, v in (rep.get("rerun") or {}).items() if k in ("agent", "a", "b", "p", "verdict")}}
    return {"error": "No finished analysis with a proven cause yet."}


def serve(history: Path, port: int = 8700, open_browser: bool = False) -> int:
    results = Results(history)

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
                return self.send(200, _page("app.html", "results"))
            if parts == ["site.css"]:
                return self.send(200, resources.files("whyagent").joinpath("site.css").read_text(encoding="utf-8"), "text/css; charset=utf-8")
            if parts == ["api", "latest"]:
                return self.json(latest(results))
            if parts == ["api", "analyses"]:
                return self.json(results.list())
            if len(parts) == 1 and parts[0] in {k for k, _, _ in SPONSORS}:
                return self.send(200, _page(parts[0] + ".html", parts[0]))
            if parts in (["api", "tiger"], ["api", "vultr"]):
                return self.json(_dashboard(parts[1]))
            if parts[0] == "live" and len(parts) <= 2:
                return self.send(200, _page("pipeline_view.html", "live"))
            if len(parts) == 3 and parts[:2] == ["api", "stream"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                def write(b: bytes):
                    self.wfile.write(b)
                    self.wfile.flush()
                try:
                    stream(results, parts[2], write)
                except OSError:  # the browser closed the page
                    pass
                return
            if len(parts) == 3 and parts[:2] == ["api", "replay"]:
                d = results.folder(parts[2])
                if d is None or not (d / "events.jsonl").exists():
                    return self.json({"error": "no saved events for this analysis"}, 404)
                from .events import load_replay
                return self.json({"id": d.name, "events": load_replay(d / "events.jsonl")})
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
                return self.send(200, _page("app.html", "results"))  # live view (or a stopped run's log)
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
    print(f"agentwhy results: {url}  (history: {history})  Ctrl-C to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
