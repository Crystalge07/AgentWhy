"""Results report: a JSON-serializable record of one analysis, rendered to a self-contained HTML page.

The CLI fills a Report as stages run; `write()` saves report.json and report.html side by side.
report.json is the raw record below. report.html is `report_template.html` with a view of it injected as
JSON (`build_view`): the shape the page's script reads (REPORT, ANALYSES, RUNS), plus the last 24 h of
runs from Tiger Data when TIGER_DATABASE_URL is set. It works offline and can be scp'd anywhere.

Raw schema (all keys optional except meta; the page shows whatever stages have data):

  meta        {repo, test, good, head, line, generated_at, command, analysis_id, sample}
  precheck    {head_fail, good_pass, n, mode, signature, fail_rate, runs_per_state}
  commit      {sha, method: "blame"|"bisect", blamed, blame_confirmed, checkpoint, human}
  session     {id, agent, prompt, replay_matches, unattributed, steps: [Step-like dicts + line, test]}
  blame_hint  {line, step_ids, outcome, is_cause, reverted}
  pruning     {pruned: [ids], candidates, verified, runs_with, runs_without}
  bisect      {probes: [{after_step, outcome}], boundary, kept, via}
  confirm     {step_id, collateral, outcome, ddmin}
  root_cause  {step_id, failing_output, collateral, extra_causes}
  lesson      {text, source: "gemini"|"brain"|"given"}
  rerun       {agent, a: [outcome], b: [outcome], p, verdict, saved_to_brain, tokens}
  live        {rule, counterfactual: [{outcome}], after: {outcome, label}}  (--live instead of lesson/rerun)
  runs        [{stage, label, outcome, duration_ms, tokens, ts, edits, cached}]
  notes       [str]
"""
from __future__ import annotations

import json
import math
import re
import shlex
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from . import events
from .types import RunResult, State

_FALLBACK_NOTE = re.compile(r"(Claude|Claude Code) (wrote it|ran them) instead|both arms reran with Claude Code")

TEMPLATE = "report_template.html"


class Report(dict):
    def __init__(self, **meta):
        super().__init__(meta={"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **meta},
                         runs=[], notes=[])
        self._seen: set[int] = set()  # RunResults already logged: the Replayer cache hands the same objects back

    def log_runs(self, stage: str, label: str, results: list[RunResult], edits: list[int] | None = None):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in results:
            cached = id(r) in self._seen  # a cache hit, not a new run of the test
            self._seen.add(id(r))
            row = {"stage": stage, "label": label, "outcome": r.outcome, "duration_ms": 0 if cached else r.duration_ms,
                   "tokens": r.tokens, "ts": now, "cached": cached}
            if edits is not None:
                row["edits"] = list(edits)
            self["runs"].append(row)

    def note(self, text: str):
        self["notes"].append(text)
        events.emit("note", text=text)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key not in ("runs", "notes", "meta"):
            events.emit("result", key=key, value=_brief(key, value))

    def to_json(self) -> str:
        return json.dumps(self, indent=2, default=lambda o: asdict(o) if is_dataclass(o) else str(o))

    def write(self, html_path: str | Path, tiger: bool = False) -> Path:
        """tiger: also read the last 24 h of runs from Tiger Data for the Run data tab (never fails the write)."""
        html_path = Path(html_path)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.with_suffix(".json").write_text(self.to_json(), encoding="utf-8")
        data = json.loads(self.to_json())
        html_path.write_text(render_html(data, tiger_runs(data) if tiger else None), encoding="utf-8")
        return html_path


def _brief(key: str, value):
    """A stage's result for the live view: small enough to stream (no diffs, no test output)."""
    if not isinstance(value, dict):
        return value
    if key == "session":
        steps = value.get("steps") or []
        return {"id": value.get("id"), "agent": value.get("agent"), "edits": len(steps),
                "sessions": len([x for x in str(value.get("id", "")).split(", ") if x]),
                "test_edits": sum(bool(x.get("test")) for x in steps)}
    if key == "root_cause":
        return {k: value.get(k) for k in ("step_id", "file", "collateral")}
    return {k: v for k, v in value.items() if k not in ("failing_output", "steps", "output")}


def render_html(data: dict, db: dict | None = None) -> str:
    """The page for one raw report. db: rows from `tiger_runs`, or None to show this analysis's runs only."""
    template = resources.files("whyagent").joinpath(TEMPLATE).read_text(encoding="utf-8")
    # </ inside a <script> block would end it early
    payload = json.dumps(build_view(data, db), ensure_ascii=False).replace("</", "<\\/")
    return template.replace("{{ DATA_JSON }}", payload)


def state_label(state: State) -> str:
    c = state.base_commit[:7]
    return f"{c}+{len(state.step_ids)} edits" if state.step_ids else c


# ---------------------------------------------------------------------------------------------------------
# The page's view of a report

# The dashboard's stages, in pipeline order. Pipeline stage names that differ map onto them.
STAGES = ("precheck", "commit", "blame_hint", "prune", "bisect", "confirm", "rerun")
_STAGE_MAP = {"blame": "blame_hint", "ddmin": "confirm"}
_RES = {"PASS": "pass", "TARGET_FAIL": "fail", "OTHER": "other"}
_STORY_DIFF_LINES = 8


def _stage(s: str) -> str:
    s = _STAGE_MAP.get(s, s)
    return s if s in STAGES else "rerun" if s.startswith("live") else "confirm"


def _lines(t: str | None) -> list[str]:
    return str(t).rstrip("\n").split("\n") if t else []


def error_line(out: str | None) -> str:
    """The one line of test output a human reads first: pytest's "E  ..." line, else the last error-looking one."""
    ls = [ln.rstrip() for ln in _lines(out) if ln.strip()]
    e = next((ln for ln in ls if re.match(r"^E\s{2,}", ln)), None)
    hit = re.sub(r"^E\s+", "", e) if e else next(
        (ln for ln in reversed(ls) if re.search(r"Error|Exception|assert|FAILED", ln)), ls[-1] if ls else "")
    return hit[:217] + "…" if len(hit) > 220 else hit


def _test_id(cmd: str) -> str:
    return next((w for w in (cmd or "").split() if "::" in w), cmd or "")


def _summary(old: str, new: str) -> str | None:
    """A factual one-liner from the diff itself: which function or class the edit touches, if it says."""
    for text in (new, old):
        m = re.search(r"^\s*(?:async\s+)?(def|class)\s+(\w+)", text or "", re.M)
        if m:
            return f"Edits {m.group(1) == 'class' and 'class ' or ''}{m.group(2)}."
    return None


def _tail(t: str, n: int = 40) -> str:
    ls = _lines(t)
    return ("…\n" if len(ls) > n else "") + "\n".join(ls[-n:])


def _blind(steps: list[dict], mode: str, n: int) -> int | None:
    """Blind bisect cost: ceil(log2(non-test edits)) states, times runs per state in flaky mode."""
    k = sum(not s.get("test") for s in steps)
    if not k:
        return None
    return math.ceil(math.log2(k)) * (n if mode == "flaky" else 1) if k > 1 else 0


def build_view(d: dict, db: dict | None = None) -> dict:
    """{report, analyses, runs, db}: what the page's script reads. Never invents values: a field the
    pipeline didn't produce is null and the page shows its empty state."""
    m = d.get("meta") or {}
    pre, cm, ses = d.get("precheck"), d.get("commit"), d.get("session") or {}
    bh, pr, bi, cf, rc = d.get("blame_hint"), d.get("pruning"), d.get("bisect"), d.get("confirm"), d.get("root_cause")
    steps = ses.get("steps") or []
    if any("test" not in st for st in steps):  # report.json from before steps carried the flag
        from .replay import is_test_file
        steps = [{**st, "test": st.get("test", is_test_file(st["file"]))} for st in steps]
    by_id = {s["id"]: s for s in steps}
    cause = by_id.get(rc["step_id"]) if rc else None
    test = _test_id(m.get("test", ""))
    test_name = test.split("::")[-1] or test
    mode = (pre or {}).get("mode")
    n_state = (pre or {}).get("runs_per_state") or 1
    aid = m.get("analysis_id") or "this"
    raw_runs = d.get("runs") or []

    # -- states the root-cause proof rests on, and the runs of each
    order = [s["id"] for s in steps if not s.get("test")]
    pruned = set(pr["pruned"]) if pr and pr.get("verified") else set()
    kept = (bi or {}).get("kept") or [i for i in order if i not in pruned]
    parent7 = next((r["label"].split("+")[0] for r in raw_runs if "+" in r.get("label", "")), None)

    def runs_of(edits: list[int] | None, stages: tuple = ()) -> list[str]:
        if edits is None:
            return []
        hit = [r for r in raw_runs if r.get("edits") == list(edits) and not r.get("cached")]
        if not hit and parent7 and not any("edits" in r for r in raw_runs):  # report.json from before edits=
            label = f"{parent7}+{len(edits)} edits"  # the pipeline labels every state this way, even +0
            hit = [r for r in raw_runs if r.get("label") == label and not r.get("cached")
                   and (not stages or r["stage"] in stages)]  # labels only count edits: keep to the stage that ran it
        return [_RES.get(r["outcome"], "other") for r in hit]

    probes = {p["after_step"]: p["outcome"] for p in (bi or {}).get("probes") or []}
    via_blame = bool(bh and bh.get("is_cause")) or (bi or {}).get("via") == "blame_hint"
    proof, stops = [], []
    if cause:
        root = cause["id"]
        k = kept.index(root) if root in kept else None
        drop = {root, *(rc.get("collateral") or (cf or {}).get("collateral") or [])}
        prev = kept[k - 1] if k else None

        prefix_stages = ("blame_hint",) if via_blame else ("bisect",)

        def row(state, has, edits, voted, expect, stages):
            runs = runs_of(edits, stages)
            res = _RES.get(voted) if voted else None
            if not res and runs:
                res = "fail" if "fail" in runs else "pass" if all(x == "pass" for x in runs) else "other"
            return {"state": state, "has": has, "runs": runs, "result": res, "expect": expect}

        before_vote = probes.get(prev if prev else 0) or ("PASS" if not prev and cm else None)
        proof = [
            row(f"Just before edit #{root}", f"The agent's edits up to #{prev}" if prev
                else "The guilty commit's parent, before any of the session's edits", kept[:k] if k is not None else None,
                before_vote, "pass", prefix_stages),
            row(f"Just after edit #{root}", "The same code plus this one edit",
                kept[:k + 1] if k is not None else None, probes.get(root), "fail", prefix_stages),
            row(f"Everything except edit #{root}", "The final code with only this edit undone"
                + (f" (and #{', #'.join(map(str, sorted(drop - {root})))}, which build on it)" if len(drop) > 1 else ""),
                [i for i in kept if i not in drop], (cf or {}).get("outcome"), "pass",
                ("blame_hint",) if via_blame else ("confirm",)),
        ]

    cidx = steps.index(cause) if cause else None
    blame_ids = set((bh or {}).get("step_ids") or [])
    if steps:
        base_ran = _RES.get(probes.get(0)) or ("pass" if cm else None)
        stops.append({"id": "base", "file": (cm or {}).get("sha", "")[:7] + "^" if cm else "base", "kind": "base",
                      "status": "pass" if cause or cm else None, "ran": base_ran,
                      "summary": "The guilty commit's parent, where the test passes." if cm else None})
    for i, s in enumerate(steps):
        kind = "test" if s.get("test") else "unreachable" if s["id"] in pruned else "code"
        is_cause = cause is not None and s["id"] == cause["id"]
        ran = probes.get(s["id"])
        stops.append({
            "id": s["id"], "file": s["file"], "line": s.get("line"), "kind": kind,
            "status": None if cidx is None else "fail" if i >= cidx else "pass",
            "ran": _RES.get(ran) if ran and kind == "code" else None,
            "tag": "cause" if is_cause else "blame" if s["id"] in blame_ids else None,
            "summary": _summary(s.get("old_text", ""), s.get("new_text", "")),
            "reaction": _tail(s["trigger"]) if s.get("trigger") else "No tool output right before this edit.",
            "reasoning": (s.get("reasoning") or "").strip() or None,
            "diff": [f"-{ln}" for ln in _lines(s.get("old_text"))] + [f"+{ln}" for ln in _lines(s.get("new_text"))],
            "command": s.get("command") or None,
            "failure": error_line(rc.get("failing_output")) if is_cause else None,
            "shellGuessed": s["id"] in (ses.get("shell_guessed") or []),
        })

    # -- git blame's pick (null when there was no --line or the hint was skipped)
    blame = None
    if bh and bh.get("step_ids"):
        f, _, ln = bh["line"].rpartition(":")
        e = by_id.get(bh["step_ids"][0], {})
        blame = {"edit": bh["step_ids"][0], "file": f, "line": int(ln) if ln.isdigit() else ln,
                 "editFile": e.get("file"), "editLine": e.get("line"), "right": bool(bh.get("is_cause"))}

    # -- the narrowing figures
    test_runs = [r for r in raw_runs if not r.get("cached") and _stage(r["stage"]) != "rerun"]
    count = lambda *st: sum(_stage(r["stage"]) in st for r in test_runs)  # noqa: E731
    narrowing = None
    if steps and (bi or pr or cf):
        narrowing = {
            "total": len(steps), "testEdits": sum(bool(s.get("test")) for s in steps), "unreachable": len(pruned),
            "pruneRuns": count("prune"), "candidates": len(kept),
            "bisectRuns": count("blame_hint") if via_blame else count("bisect"), "viaBlame": via_blame,
            "totalRuns": len(test_runs), "seconds": round(sum(r.get("duration_ms") or 0 for r in test_runs) / 1000, 1),
            "mode": mode, "failRate": (pre or {}).get("fail_rate"), "runsPerState": (pre or {}).get("runs_per_state"),
        }

    # -- lesson: stages 7-8, or the --live loop's rule
    lesson, lesson_error = None, None
    ls, rr, live = d.get("lesson"), d.get("rerun"), d.get("live")
    arm = lambda a: [sum(o == "PASS" for o in a), sum(o != "OTHER" for o in a)]  # noqa: E731
    if ls:
        lesson = {"text": ls["text"], "source": ls.get("source"), "without": None, "with": None, "p": None,
                  "verdict": None, "validated": False, "saved": False}
        if rr and (rr.get("a") or rr.get("b")):
            lesson.update(without=arm(rr["a"]), **{"with": arm(rr["b"])})
        if rr:
            lesson.update(p=rr.get("p"), verdict=rr.get("verdict"), validated=rr.get("verdict") == "VALIDATED",
                          saved=bool(rr.get("saved_to_brain")), agent=rr.get("agent"))
    elif live and live.get("rule"):
        after = (live.get("after") or {}).get("outcome")
        lesson = {"text": live["rule"], "source": "live", "without": arm([c["outcome"] for c in live.get("counterfactual", [])]),
                  "with": arm([after]) if after else None, "p": None, "verdict": None, "validated": False,
                  "saved": False, "live": (live.get("after") or {}).get("label")}
    # Which model/agent stood in for Gemini is plumbing, not a result: hide old fallback notes.
    d = {**d, "notes": [x for x in d.get("notes") or [] if not _FALLBACK_NOTE.search(x)]}
    failed = [x for x in d.get("notes", []) if re.match(r"(Lesson|Reruns skipped|Live loop failed)", x)]
    if not lesson:
        lesson_error = failed[0] if failed else ("Not run (--no-rerun)." if cause else "No root cause, so no lesson.")
    lesson_error = lesson_error and (lesson_error if lesson_error.endswith(".") else lesson_error + ".")

    # -- the headline when there's no proven root cause
    if cause:
        headline = None
    elif cm and cm.get("human"):
        headline = f"Commit {cm['sha'][:7]} broke {test_name}, and a person wrote it: it has no Entire checkpoint."
    elif cm:
        headline = f"Commit {cm['sha'][:7]} broke {test_name}. The edit-level search didn't finish."
    elif pre:
        headline = f"The analysis stopped at the precheck: HEAD failed {pre['head_fail']}/{pre['n']} runs, the good commit passed {pre['good_pass']}/{pre['n']}."
    else:
        headline = "No results yet."

    notes = list(d.get("notes") or [])
    if m.get("sample"):
        notes.insert(0, "Sample data for layout. This report was not produced by a real run.")

    when = m.get("generated_at", "")
    try:
        when = datetime.fromisoformat(when).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        pass
    good, head = (m.get("good") or "")[:7], (m.get("head") or "")[:7]
    command = m.get("command") or " ".join(
        ["agentwhy", "--test", shlex.quote(m.get("test", ""))] + (["--good", good] if good else [])
        + (["--line", m["line"]] if m.get("line") else []))
    report = {
        "id": aid, "repo": m.get("repo") or "", "range": f"{good}..{head}" if good or head else "", "when": when,
        "command": command, "test": test, "testName": test_name,
        "agent": ses.get("agent") or "", "session": (ses.get("id") or "")[:8], "commit": (cm or {}).get("sha", "")[:7],
        "cause": {"edit": cause["id"], "file": cause["file"], "line": cause.get("line")} if cause else None,
        "blame": blame, "task": ses.get("prompt") or "", "proof": proof, "stops": stops, "narrowing": narrowing,
        "lesson": lesson, "lessonError": lesson_error, "notes": notes, "headline": headline,
        "failure": error_line(rc.get("failing_output")) if rc else None,
    }

    # -- run data: Tiger's last 24 h, else this analysis's runs from memory
    own = [{"ts": r.get("ts") or m.get("generated_at"), "analysis": aid, "stage": _stage(r["stage"]),
            "state": r.get("label", ""), "outcome": r["outcome"],
            "dur": None if _stage(r["stage"]) == "rerun" and not r.get("duration_ms") else (r.get("duration_ms") or 0) / 1000,
            "tokens": r.get("tokens") or 0} for r in raw_runs if not r.get("cached")]
    if rr and not any(_stage(r["stage"]) == "rerun" for r in raw_runs):  # reruns aren't in report.runs
        own += [{"ts": m.get("generated_at"), "analysis": aid, "stage": "rerun", "state": f"arm {a}", "outcome": o,
                 "dur": None, "tokens": 0} for a, arm_ in (("A", rr.get("a") or []), ("B", rr.get("b") or [])) for o in arm_]
    this = {"label": f"{aid[:6]} · {test_name}", "test": test_name, "blind": _blind(steps, mode, n_state)}
    if db and db.get("runs") is not None:
        runs = list(db["runs"])
        if not any(r["analysis"] == aid for r in runs):
            runs += own  # --live, or Tiger logging dropped mid-analysis
        analyses = dict(db.get("analyses") or {})
        analyses[aid] = {**analyses.get(aid, {}), **this}
        connected = True
    else:
        runs, analyses, connected = own, {aid: this}, False
    for r in runs:
        analyses.setdefault(r["analysis"], {"label": r["analysis"][:6], "test": "", "blind": None})
    return {"report": report, "analyses": analyses, "runs": runs, "db": connected}


# ---------------------------------------------------------------------------------------------------------
# Tiger Data: the Run data tab

RUNS_SQL = """
SELECT r.time, r.analysis_id, r.stage, r.state_hash, r.outcome, r.duration_ms, coalesce(r.tokens, 0),
       a.label, a.blind_runs
FROM runs r LEFT JOIN analyses a USING (analysis_id)
WHERE r.time > now() - interval '24 hours' AND NOT r.cached
ORDER BY r.time DESC
LIMIT 2000
"""


def tiger_runs(data: dict) -> dict | None:
    """{runs, analyses} for the last 24 h, or None if Tiger isn't configured or the query fails.
    Stores this analysis's blind bisect estimate on its `analyses` row first, so later reports can show it."""
    from . import tiger

    try:
        conn = tiger.connect()
    except Exception as e:  # noqa: BLE001
        print(f"report: Tiger Data unavailable ({type(e).__name__}), showing this run only", file=sys.stderr)
        return None
    if conn is None:
        return None
    try:
        with conn:
            conn.execute(tiger.ANALYSES_DDL)
            conn.execute(tiger.BLIND_DDL)
            aid = (data.get("meta") or {}).get("analysis_id")
            pre = data.get("precheck") or {}
            blind = _blind((data.get("session") or {}).get("steps") or [], pre.get("mode"), pre.get("runs_per_state") or 1)
            if aid and blind is not None:
                conn.execute("UPDATE analyses SET blind_runs = %s WHERE analysis_id = %s", (blind, aid))
            rows = conn.execute(RUNS_SQL).fetchall()
    except Exception as e:  # noqa: BLE001 — the report is written either way
        print(f"report: Tiger Data query failed ({type(e).__name__}: {e}), showing this run only", file=sys.stderr)
        return None
    runs, analyses = [], {}
    for t, aid, stage, state, outcome, ms, tokens, label, blind in reversed(rows):
        runs.append({"ts": t.astimezone(timezone.utc).isoformat(timespec="seconds"), "analysis": aid,
                     "stage": _stage(stage), "state": (state or "")[:7], "outcome": outcome,
                     "dur": (ms or 0) / 1000, "tokens": tokens or 0})
        if aid not in analyses:
            name = _test_id((label or "").partition(": ")[2]).split("::")[-1] or (label or aid)
            analyses[aid] = {"label": f"{aid[:6]} · {name}", "test": name, "blind": blind}
    return {"runs": runs, "analyses": analyses}
