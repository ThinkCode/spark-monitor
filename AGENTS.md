# Setting this up with a coding agent

This file is written for an AI coding agent (Claude Code, Codex, Cursor, or
similar) that has shell access to the machine you want to run the dashboard on.
Point your agent at this repository and it should be able to install and
configure Spark Monitor without further help.

Humans: `docs/QUICKSTART.md` is the shorter path. Nothing here is required.

---

## What you are configuring

A zero-dependency Python dashboard for a small GPU cluster. One process on one
node reaches the others over SSH and serves a web UI. There is no database, no
agent on the other nodes, and nothing to install beyond Python 3.

**Constraints that are not negotiable — check them before proposing changes:**

- **Standard library only.** CI fails the build on any third-party import
  (`.github/assert_stdlib_only.py`). Do not add dependencies. Do not suggest a
  virtualenv full of packages.
- **One write endpoint.** The HTTP server accepts writes at `/api/settings`
  and nowhere else. Do not add control endpoints without the maintainer asking
  for them; the read-only surface is a deliberate security property, not an
  oversight.
- **No real addresses in the repo.** CI fails on any IP that is not a known
  documentation example, on tailnet addresses, and on email addresses
  (`.github/check_no_pii.py`). Use the examples already in the docs.

## Setup, in order

### 1. Confirm the environment

```bash
python3 --version          # 3.9+
uname -sm                  # Linux aarch64 on a Spark/GX10
nvidia-smi --query-gpu=name --format=csv,noheader   # may be absent; fine
```

The dashboard runs anywhere. Collectors that find nothing return empty and the
UI degrades gracefully — that path is covered by CI, so do not add guards for
"no GPU" as if it were a bug.

### 2. Decide where it runs

Pick one node as the host. It needs **passwordless SSH to every other node**;
the others need nothing installed. Verify before configuring, because almost
every "no data" report is really this:

```bash
ssh -o BatchMode=yes user@<other-node> uname -r
```

If that prompts or fails, fix SSH keys first. See `docs/MULTI-NODE.md`.

### 3. Write the config

Copy `nodes.example.json` to `~/.config/spark-monitor/config.json` and edit.
Ask the user for their node addresses rather than scanning the network. A
minimal config is:

```json
{
  "cluster_name": "My Cluster",
  "nodes": [
    { "id": "n1", "name": "node-1", "role": "head",   "host": null },
    { "id": "n2", "name": "node-2", "role": "worker", "host": "10.0.0.12" }
  ],
  "ssh_user": null,
  "serve_ports": [8000, 8080, 8888]
}
```

- `host: null` means the machine running the dashboard — exactly one node
  should have it.
- `ssh_user: null` means the user the dashboard runs as.
- `serve_ports` should list only ports you actually use; each is probed per
  node.

Full reference: `docs/CONFIGURATION.md`.

### 4. Run it

```bash
python3 spark-monitor.py            # foreground, for a first look
```

Then install as a service using `contrib/spark-monitor.service`; see
`docs/INSTALL.md`. Do not write a new unit file from scratch — the shipped one
already sets the sandboxing this project expects.

### 5. Verify, do not assume

```bash
curl -fsS localhost:8088/healthz
curl -fsS localhost:8088/api/stats | head -c 400
curl -fsS localhost:8088/api/firmware | head -c 400
```

If `/api/stats` shows nodes but no GPU numbers, the SSH hop is working and the
collectors are not — check `nvidia-smi` on the node itself before changing
config.

## Firmware drawer

Works with no extra configuration; it uses the same node list and SSH settings.
The first sweep runs `fwupdmgr` on every node and takes tens of seconds, so it
is cached and refreshed in the background — an empty drawer on first open is
normal, not a failure.

It reports versions and generates the root commands to apply updates. **It does
not install anything, and should not be made to.** If the user asks you to
automate firmware installs, explain the tradeoff first: a failed UEFI or
embedded-controller capsule can leave a machine that will not boot, and no
remote orchestration recovers that. `docs/FIRMWARE.md` has the details,
including measured reboot times.

To cover something the dashboard cannot reach itself — a managed switch, a UPS
— set `firmware_report` to a JSON file path and have any script write it. The
dashboard only reads it.

## Common failure modes

| Symptom | Cause, in order of likelihood |
|---|---|
| Node shows as unreachable | Passwordless SSH is not set up from the host node |
| Node reachable, no GPU data | `nvidia-smi` missing or failing on that node |
| Firmware drawer empty | First sweep still running; re-open in ~30 s |
| Firmware shows no updates ever | `fwupdmgr` not installed on the nodes |
| Package updates look stale | apt index is old; refreshing needs root. The drawer prints its age |
| Dashboard reachable but buttons inert | Some builds gate controls by client network; check the page for a read-only notice |

## Things not to do

- Do not add third-party packages, however convenient. CI enforces this.
- Do not commit real IPs, hostnames, tailnet addresses, or emails. CI enforces
  this too, but the check runs after you have already written the file.
- Do not add a write endpoint to "make it easier". Ask first.
- Do not run the CI scripts as a substitute for testing the actual change;
  they check hygiene, not behaviour.

## Before you commit

```bash
python3 .github/assert_stdlib_only.py
python3 .github/check_no_pii.py
python3 .github/check_embedded_js.py
bash    .github/smoke-test.sh
```

All four must pass. `smoke-test.sh` starts the server with a GPU-less config
and exercises every endpoint, which is the graceful-degradation path.
