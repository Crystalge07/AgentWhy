"""Tiger Data chart: test runs to reach the root cause, Graph pruning on vs off, per bug.

    whyagent --chart            # writes .whyagent/chart.html from TIGER_DATABASE_URL

Reads the `runs_by_stage` continuous aggregate (refreshed first), pairing each bug's latest pruned and
latest unpruned analysis. Reruns are agent runs, not test runs, so they're left out of the count.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

FETCH = """
SELECT a.label, a.pruned, a.analysis_id, a.started, r.stage,
       sum(r.test_runs)::int AS runs, coalesce(sum(r.tokens), 0)::bigint AS tokens
FROM analyses a JOIN {source} r USING (analysis_id)
WHERE r.stage <> 'rerun'
GROUP BY 1, 2, 3, 4, 5
ORDER BY 4, 5
"""

# Stages pruning can change. Precheck and commit bisect cost the same either way, so they only dilute it.
EDIT_STAGES = ("blame_hint", "prune", "bisect", "confirm", "ddmin")


def bug_name(label: str) -> str:
    """'repo: <any test command> tests/x.py::test_y' -> 'repo: tests/x.py::test_y', so the same bug pairs up
    whether the command was `pytest ...`, `python -m pytest ...` or a full interpreter path (Windows/Mac)."""
    repo, _, cmd = label.partition(": ")
    node = next((w for w in cmd.split() if "::" in w), None)
    return f"{repo}: {node.replace(chr(92), '/')}" if node and cmd else label


RAW = "(SELECT analysis_id, stage, (count(*) FILTER (WHERE NOT cached)) AS test_runs, sum(tokens) AS tokens" \
      " FROM runs GROUP BY analysis_id, stage)"


def fetch(conn) -> list[dict]:
    """One dict per (bug, pruned) pair, the latest analysis of each that got to edit-level search (bisect or
    pruning; a blame-hint hit or a reruns-only run has nothing to compare):
    {label, pruned, analysis_id, started, stages: {stage: runs}}."""
    try:
        conn.execute("CALL refresh_continuous_aggregate('runs_by_stage', NULL, NULL)")
        rows = conn.execute(FETCH.format(source="runs_by_stage")).fetchall()
    except Exception:  # no permission to refresh, or no aggregate: same numbers from the raw table
        rows = conn.execute(FETCH.format(source=RAW)).fetchall()
    per: dict[str, dict] = {}
    for label, pruned, aid, started, stage, runs, _tokens in rows:
        d = per.setdefault(aid, {"label": bug_name(label), "pruned": pruned, "analysis_id": aid,
                                 "started": started.isoformat(timespec="minutes"), "stages": {}})
        d["stages"][stage] = runs
    out: dict[tuple, dict] = {}
    for d in sorted(per.values(), key=lambda d: d["started"]):  # later analyses replace earlier ones
        if "bisect" in d["stages"] or "prune" in d["stages"]:
            out[(d["label"], d["pruned"])] = d
    return list(out.values())


def render(rows: list[dict], generated: str | None = None) -> str:
    data = json.dumps({"rows": rows, "generated": generated or datetime.now(timezone.utc).isoformat(timespec="minutes")})
    return TEMPLATE.replace("/*__DATA__*/null", data.replace("</", "<\\/"))


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Graph pruning savings</title>
<style>
:root { --bg:#fcfcfb; --panel:#ffffff; --ink:#0b0b0b; --ink2:#52514e; --muted:#8a8984; --line:#e6e5e0;
  --blind:#eb6834; --pruned:#2a78d6; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#1a1a19; --panel:#222220; --ink:#f0efec;
  --ink2:#c3c2b7; --muted:#8f8e88; --line:#34332f; --blind:#d95926; --pruned:#3987e5; } }
:root[data-theme="dark"] { --bg:#1a1a19; --panel:#222220; --ink:#f0efec; --ink2:#c3c2b7; --muted:#8f8e88;
  --line:#34332f; --blind:#d95926; --pruned:#3987e5; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif; }
main { max-width:880px; margin:0 auto; padding:28px 16px 56px; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-.01em; }
.sub { color:var(--ink2); margin:0 0 20px; }
.hero { display:flex; flex-wrap:wrap; gap:8px 28px; align-items:baseline; margin-bottom:20px; }
.hero b { font-size:40px; font-weight:700; letter-spacing:-.02em; }
.hero span { color:var(--ink2); }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:18px; }
.legend { display:flex; gap:18px; font-size:13px; color:var(--ink2); margin-bottom:14px; }
.legend i { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px; vertical-align:-1px; }
.bug { margin-bottom:18px; }
.bug:last-child { margin-bottom:0; }
.bug-name { font-size:13px; font-family:ui-monospace, Menlo, monospace; color:var(--ink2); margin-bottom:6px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.row { display:grid; grid-template-columns:92px 1fr; gap:10px; align-items:center; height:26px; }
.row .k { font-size:12px; color:var(--muted); }
.track { position:relative; height:16px; }
.bar { position:absolute; left:0; top:0; height:16px; border-radius:0 4px 4px 0; cursor:default; }
.val { position:absolute; top:-1px; font-size:13px; font-weight:600; color:var(--ink); white-space:nowrap; }
.missing { font-size:12px; color:var(--muted); }
#tip { position:fixed; pointer-events:none; background:var(--panel); color:var(--ink); border:1px solid var(--line);
  border-radius:8px; padding:8px 10px; font-size:12px; box-shadow:0 4px 16px rgba(0,0,0,.12); display:none; z-index:5; }
#tip table td { padding:0 8px 0 0; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--ink2); margin:28px 0 10px; font-weight:600; }
table.data { width:100%; border-collapse:collapse; font-size:13px; }
table.data th { text-align:left; color:var(--ink2); font-weight:600; padding:6px 8px; border-bottom:1px solid var(--line); }
table.data td { padding:5px 8px; border-bottom:1px solid var(--line); }
td.n { text-align:right; font-variant-numeric:tabular-nums; }
.empty { color:var(--ink2); }
.foot { color:var(--muted); font-size:12px; margin-top:16px; }
</style></head>
<body><main id="app"></main><div id="tip"></div>
<script id="data" type="application/json">/*__DATA__*/null</script>
<script>
(() => {
const D = JSON.parse(document.getElementById("data").textContent) || {rows: []};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const EDIT = new Set(["blame_hint", "prune", "bisect", "confirm", "ddmin"]);
const total = r => Object.entries(r.stages).reduce((a, [s, n]) => a + (EDIT.has(s) ? n : 0), 0);
const bugs = {};
for (const r of D.rows) (bugs[r.label] ||= {})[r.pruned ? "pruned" : "blind"] = r;
const labels = Object.keys(bugs).sort();
const paired = labels.filter(l => bugs[l].pruned && bugs[l].blind);
const sum = k => paired.reduce((a, l) => a + total(bugs[l][k]), 0);
const max = Math.max(1, ...D.rows.map(total));
let h = `<h1>Graph pruning savings</h1><p class="sub">Test runs whyagent needed to find and prove the guilty edit inside the agent session (pruning, bisect, confirm), with Graph pruning on vs plain bisect. Precheck and commit search cost the same either way and are left out. Logged in Tiger Data.</p>`;
if (!labels.length) {
  h += `<div class="panel empty">No analyses logged yet. Run whyagent with <code>TIGER_DATABASE_URL</code> set, once normally and once with <code>--no-prune</code>.</div>`;
} else {
  if (paired.length) {
    const b = sum("blind"), p = sum("pruned"), pct = b ? Math.round((b - p) / b * 100) : 0;
    h += `<div class="hero"><b>${pct > 0 ? "−" + pct + "%" : pct === 0 ? "0%" : "+" + -pct + "%"}</b><span>${b} → ${p} test runs across ${paired.length} bug${paired.length > 1 ? "s" : ""}</span></div>`;
  }
  h += `<div class="panel"><div class="legend"><span><i style="background:var(--blind)"></i>Plain bisect (--no-prune)</span><span><i style="background:var(--pruned)"></i>Graph pruning</span></div>`;
  for (const l of labels) {
    h += `<div class="bug"><div class="bug-name" title="${esc(l)}">${esc(l)}</div>`;
    for (const [k, name] of [["blind", "plain bisect"], ["pruned", "Graph pruning"]]) {
      const r = bugs[l][k];
      h += `<div class="row"><span class="k">${name}</span><div class="track">` + (r
        ? `<div class="bar" data-l="${esc(l)}" data-k="${k}" style="width:calc(${total(r) / max * 100}% - 36px);background:var(--${k})"></div><span class="val" style="left:calc(${total(r) / max * 100}% - 30px)">${total(r)}</span>`
        : `<span class="missing">not run yet${k === "blind" ? " (run with --no-prune)" : ""}</span>`) + `</div></div>`;
    }
    h += `</div>`;
  }
  h += `</div><h2>Runs by stage</h2><table class="data"><thead><tr><th>Bug</th><th>Mode</th><th>Stage</th><th style="text-align:right">Test runs</th></tr></thead><tbody>`;
  for (const l of labels) for (const k of ["blind", "pruned"]) {
    const r = bugs[l][k]; if (!r) continue;
    for (const [st, n] of Object.entries(r.stages)) h += `<tr><td>${esc(l)}</td><td>${k === "blind" ? "plain bisect" : "Graph pruning"}</td><td>${esc(st)}</td><td class="n">${n}</td></tr>`;
  }
  h += `</tbody></table>`;
}
h += `<div class="foot">Latest analysis per bug and mode · generated ${esc(D.generated)}</div>`;
document.getElementById("app").innerHTML = h;
const tip = document.getElementById("tip");
document.querySelectorAll(".bar").forEach(el => {
  el.addEventListener("mousemove", e => {
    const r = bugs[el.dataset.l][el.dataset.k];
    tip.innerHTML = `<b>${el.dataset.k === "blind" ? "Plain bisect" : "Graph pruning"}: ${total(r)} runs</b><table>` +
      Object.entries(r.stages).map(([s, n]) => `<tr><td>${esc(s)}</td><td>${n}</td></tr>`).join("") + `</table>`;
    tip.style.display = "block";
    tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = (e.clientY + 14) + "px";
  });
  el.addEventListener("mouseleave", () => tip.style.display = "none");
});
})();
</script></body></html>
"""
