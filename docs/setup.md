# Infra setup (owner: B)

## Vultr — sandbox VM

1. **Credits.** Sign up at vultr.com and redeem the HackTrent promo code/credit link from the sponsor table or Discord. Credits can take a while to approve, so do this first.
2. **SSH key.** Account → SSH Keys → add `~/.ssh/id_ed25519.pub` (create with `ssh-keygen -t ed25519` if missing).
3. **Deploy.** Products → Deploy → Cloud Compute (Shared CPU is fine):
   - OS: Ubuntu 24.04 LTS (or Marketplace → Docker, which preinstalls it)
   - Region: closest to the venue
   - Size: 4 vCPU / 8 GB to start (≈ 4 workers). Resize to 8 vCPU for the demo if credits allow.
   - Attach your SSH key; hostname `whyagent`.
4. **Install Docker** (skip if you used the Docker marketplace image):
   ```sh
   ssh root@<IP>
   curl -fsSL https://get.docker.com | sh
   docker run --rm hello-world
   ```
5. **Firewall.** Network → Firewall: allow inbound 22 only. Sandboxes need no inbound ports.
6. **Run whyagent on the VM itself**, not via a remote Docker context — sandboxes bind-mount git worktrees, and those paths must exist on the Docker host.
   ```sh
   apt-get install -y git python3-venv
   git clone <this repo> && cd agentwhy
   python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
   # also clone the seeded demo repo, install the Entire CLI, and set GEMINI_API_KEY / TIGER_DATABASE_URL
   ```
7. **Stop billing** when idle: power off doesn't stop charges — snapshot and destroy between sessions, or just watch the credit balance.

## Tiger Data — run log + chart

1. Sign up at tigerdata.com (formerly Timescale) and create a **service** (PostgreSQL + TimescaleDB). The free tier/trial is plenty.
2. Copy the connection string and export it:
   ```sh
   export TIGER_DATABASE_URL='postgres://tsdbadmin:<pw>@<host>:<port>/tsdb?sslmode=require'
   ```
3. Apply the schema:
   ```sh
   psql "$TIGER_DATABASE_URL" -f docs/tiger_schema.sql
   ```
4. Python side: `pip install 'psycopg[binary]'`. `replay.run` calls `log_run(stage, state_hash, result)` for every test run; reruns log with `stage='rerun'` and `tokens` set. Make `log_run` a no-op when `TIGER_DATABASE_URL` is unset so tests and offline runs still work.
5. **The chart:** run the same analysis with and without `--no-prune` (each gets its own `analysis_id` and `pruned` flag), then query `runs_by_stage` in the Tiger console or a notebook.
