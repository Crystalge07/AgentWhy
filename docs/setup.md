# Infra setup

## Vultr — sandbox VM

1. **Account.** Sign up at vultr.com.
2. **SSH key.** Account → SSH Keys → add `~/.ssh/id_ed25519.pub` (create with `ssh-keygen -t ed25519` if missing).
3. **Deploy.** Products → Deploy → Cloud Compute (Shared CPU is fine):
   - OS: Ubuntu 24.04 LTS (or Marketplace → Docker, which preinstalls it)
   - Region: closest to you
   - Size: 4 vCPU / 8 GB to start (≈ 4 workers). 8 vCPU for more parallel workers.
   - Attach your SSH key; hostname `agentwhy`.
4. **Install Docker** (skip if you used the Docker marketplace image):
   ```sh
   ssh root@<IP>
   curl -fsSL https://get.docker.com | sh
   docker run --rm hello-world
   ```
5. **Firewall.** Network → Firewall: allow inbound 22 only. Sandboxes need no inbound ports.
6. **Run agentwhy on the VM itself**, not via a remote Docker context — sandboxes bind-mount git worktrees, and those paths must exist on the Docker host.
   ```sh
   apt-get install -y git python3-venv
   git clone <this repo> && cd agentwhy
   python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
   # also clone the repo you want to analyze, install the Entire CLI, and set GEMINI_API_KEY / TIGER_DATABASE_URL
   ```
   **Auto-sync your code to the VM** (Mac, Linux or Windows; needs your SSH key on the VM). One time per machine, from the repo root:
   ```sh
   .venv/bin/python scripts/sync_vm.py --install-hook root@<VM-IP>            # Mac/Linux
   .venv\Scripts\python scripts\sync_vm.py --install-hook root@<VM-IP>        # Windows
   ```
   After that, every `git commit` copies tracked files to `/root/hacktrent/agentwhy` and reinstalls them in the background. The VM's `.env` and `.venv` are left alone. The log is `.git/whyagent-vm-sync.log`. Run `scripts/sync_vm.py` to sync by hand, or `git config whyagent.vmSync false` to turn it off.
7. **Stop billing** when idle: power off doesn't stop charges — snapshot and destroy between sessions.

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
4. Python side: `pip install 'psycopg[binary]'`. Every test run is logged to `runs` (`whyagent/tiger.py`, `RunLog`); reruns log with `stage='rerun'` and `tokens` set. With `TIGER_DATABASE_URL` unset, logging is off and everything else still works. agentwhy creates the tables itself if step 3 was skipped, but only the schema file adds the hypertable and the `runs_by_stage` view the chart needs.
5. **The chart:** run the same analysis with and without `--no-prune` (each gets its own `analysis_id` and `pruned` flag), then query `runs_by_stage` in the Tiger console or a notebook.
