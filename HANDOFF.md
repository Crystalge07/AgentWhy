# Handoff: wire the new report design into whyagent

Attached: `report.html`, a static design prototype of the whyagent HTML report. It has two tabs: **Report** (one analysis) and **Run data** (a Tiger Data dashboard). Every value in it is hardcoded. Your job is to make the real pipeline generate this page from real data, replacing the current report template.

Read `report.html` fully before starting, then find the existing report generator in this repo (the code that writes the self-contained HTML report + JSON at the end of a run).

## Ground rules

- **Keep the design exactly as is.** That means the layout, CSS, copy and colours. Only change markup where the data requires it (e.g. variable number of edits, proof rows, notes). Don't add gradients, icons, emoji, animations, or new sections.
- The output must stay **one self-contained HTML file**. The only external request allowed is the Google Fonts link that's already there, and the page must still look fine if it's offline (it falls back to system fonts).
- Don't use em dashes in any user-facing copy you add. Use colons, commas or periods.
- Don't fabricate values. If the pipeline doesn't produce a field, render the empty/fallback state (see below) rather than inventing it.
- The page has no build step: no bundler, no framework. The generator should inject data as JSON into the page, and the existing JS in the page renders it.

## Step 1: Turn the page into a template

1. Move `report.html` into the repo as the report template (e.g. `whyagent/report/template.html`, wherever the current template lives).
2. In the `<script>`, replace the hardcoded `const REPORT = {...}`, `const ANALYSES = {...}` and the `const RUNS = (() => {...})()` sample generator with a single injected data block:

   ```html
   <script id="whyagent-data" type="application/json">{{ DATA_JSON }}</script>
   ```
   and at the top of the main script:
   ```js
   const DATA = JSON.parse(document.getElementById("whyagent-data").textContent);
   const REPORT = DATA.report;
   const ANALYSES = DATA.analyses;
   const RUNS = DATA.runs.map((r) => ({ ...r, ts: new Date(r.ts) }));
   ```
3. When serialising, escape `</` as `<\/` in the JSON so a diff or error message containing `</script>` can't break the page.
4. Delete the sample-data note (`#sampleNote`) when real runs are present. Keep it, with the text changed to "Tiger Data not configured, showing this run only", when there's no DB connection (see Step 3).

## Step 2: Map pipeline output to `REPORT`

Fill every field of `REPORT` from the analysis result. The shape the page expects:

| Field | Source in the pipeline |
| --- | --- |
| `id`, `repo`, `range`, `when` (UTC), `command` | analysis metadata / CLI args. `command` = the exact invocation to rerun this analysis |
| `test`, `testName` | the target test id, and its function name |
| `agent`, `session`, `commit` | stage 1–2: guilty commit + Entire session id |
| `cause: {edit, file, line}` | stage 6 confirmed root cause |
| `blame: {edit, file, line}` | stage 3 blame hint (the edit git blame pointed at). `null` if there was no `--line` |
| `task` | the user prompt the agent was working from, from the session |
| `proof[]` | stage 6: the three states (just before cause, just after, final minus cause). `runs` = list of `"pass"`/`"fail"` per actual run (5 entries in flaky mode) |
| `stops[]` | stage 2 replayed edits, in order, with a leading `base` stop. Per edit: `id`, `file`, `line`, `kind` (`code` / `test` / `unreachable`), `status` (pass/fail of the state right after it: known from runs or inferred from the bisect boundary), `ran` (`"pass"`/`"fail"` only if that exact state was executed, else `null`), `tag` (`cause` / `blame` / null), `summary`, `reaction` (trigger: tool output the agent saw), `reasoning` (null if hidden), `diff` (list of `-`/`+` lines), `command` (if made via shell), `failure` (only on the cause) |
| `narrowing` | counts from stages 2–5: `total`, `testEdits`, `unreachable`, `pruneRuns`, `candidates`, `bisectRuns`, `totalRuns`, `seconds`, `mode` (`stable`/`flaky`) |
| `lesson` | stage 7–8: `{text, without:[passes, n], with:[passes, n], p, validated}` or `null` |
| `lessonError` | the error string if stage 7/8 failed, else null |
| `notes[]` | the report's existing warnings |

Things to watch:
- **Blame line mismatch.** The prototype has the blame hint at `client.py:39` in one place and `client.py:16` on the timeline. Use the real values: `blame.line` is the line passed to `--line`, and the timeline stop shows the line that edit actually touched. Make the copy say which is which if they differ.
- The story strip shows the first two sentences of `task`. Fine for long tasks; for one-sentence tasks it should show the whole thing.
- Truncate very long diffs in the story strip (first ~8 lines), but show them in full in the detail panel.
- If `blame` is null, hide the git blame half of the comparison row and the `GIT BLAME` flag.
- In flaky mode, change the figure "fails every time at HEAD" to the measured failure rate.

## Step 3: Run data tab (Tiger Data)

The pipeline already logs every test run and rerun to Tiger Data (`psycopg`). Use that table to fill `DATA.runs` and `DATA.analyses`.

1. Check the actual table and continuous aggregate names in this repo. The SQL shown in the page's "Query" dropdowns assumes `test_runs`, `runs_by_stage`, and a `blind_estimate` column. **Update those SQL snippets in the template to match the real schema**, because judges will read them.
2. If there's no `blind_estimate`, compute blind bisect cost when writing the report: `ceil(log2(non-test edits))` runs for a stable bug, times runs-per-state (5) in flaky mode. Store it per analysis in `ANALYSES[id].blind`.
3. Each run row needs: `ts` (ISO string), `analysis`, `stage` (one of `precheck, commit, blame_hint, prune, bisect, confirm, rerun`, mapped from the pipeline's stage names), `state` (short state hash), `outcome` (`PASS` / `TARGET_FAIL` / `OTHER`), `dur` (seconds), `tokens` (0 for test runs).
4. Query the last 24h of runs across all analyses (cap at ~2000 rows), so the dashboard shows more than the current analysis. `ANALYSES` = one entry per analysis id in those rows: `{label, test, blind}`.
5. If the DB isn't configured or the query fails, don't crash the report. Fall back to this analysis's own runs from memory and show the "not configured" note.

## Step 4: Verify

- Generate the report for all three recorded demo scenarios (obvious, sneaky, flaky) and open each one.
- Check, at 1320px and 390px wide: no horizontal page scroll (only the timeline strip and graph may scroll inside their boxes), nothing overlapping, and the root cause node visible on the timeline.
- Click through every timeline stop, use the arrow keys, flip the "Only ones that matter" filter, switch tabs, change both dashboard filters, and hover the chart. Check the browser console has no errors.
- Check the numbers agree across the page: `narrowing.totalRuns` = the row count in the technical-details table = the dashboard's test runs for that analysis.
- Run the existing test suite. Add a test that renders the template with a fixture and asserts the JSON round-trips and contains no `</script>`.

## Don't touch

The analysis pipeline itself (stages 0–8). This task is presentation and data plumbing only. If a field the page needs isn't available, list it at the end instead of changing pipeline logic.
