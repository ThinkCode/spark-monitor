# Configuration

Everything is one JSON file. Nothing in `spark-monitor.py` needs editing, ever.

## Where the file lives

Searched in this order, first match wins:

1. `--config /path/to/config.json`
2. `$SPARK_MONITOR_CONFIG`
3. `~/.config/spark-monitor/config.json` (or `$XDG_CONFIG_HOME/spark-monitor/config.json`)

If none exists, Spark Monitor writes the third one on first run, describing the machine
it is on, and prints where it put it.

**Restart after editing:** `systemctl --user restart spark-monitor`.

Any key you leave out gets its default. Unknown keys are ignored rather than treated as
an error, so a config from a newer version won't stop an older one from starting.

## The smallest useful config

```json
{
  "nodes": [
    { "name": "spark-1", "role": "head" },
    { "name": "spark-2", "host": "10.0.0.12" }
  ]
}
```

## A full example

```json
{
  "cluster_name": "Home Lab",
  "port": 8088,
  "bind": "0.0.0.0",

  "nodes": [
    { "id": "n1", "name": "spark-1", "role": "head",   "host": null },
    { "id": "n2", "name": "spark-2", "role": "worker", "host": "10.0.0.12" },
    { "id": "n3", "name": "gx10-1",  "role": "worker", "host": "10.0.0.13" }
  ],

  "ssh_user": null,
  "serve_ports": [8000, 8080, 8888],
  "router_port": null,
  "fabric_prefix": null,

  "engine_patterns": ["vllm", "llama", "sglang", "tgi"],

  "power": { "price_kwh": 0.28, "currency": "£", "idle_w": 48, "load_w": 205 },

  "history_days": 45,
  "sample_seconds": 60,

  "catalog": {
    "scan": ["~/.cache/huggingface/hub", "~/models", "~/models/*.gguf"],
    "shared_storage": { "mount": "/mnt/nas", "sections": ["models", "backups"] },
    "pinned": { "Llama-3.3-70B": "sharded with TP=2, both nodes need it" }
  }
}
```

---

## Reference

### `cluster_name`

*String, default `"Spark Cluster"`.* Shown as the page heading and browser tab title.
Cosmetic.

### `port`

*Integer, default `8088`.* Overridable at runtime with `--port`.

### `bind`

*String, default `"0.0.0.0"`.* Which address to listen on.

`0.0.0.0` accepts connections from anywhere that can route to the machine, which is what
makes the dashboard reachable from your laptop and phone. **There is no authentication**,
so this is only safe on a network you trust — see [SECURITY.md](../SECURITY.md).

Set `"127.0.0.1"` to accept only local connections; you then reach it through an SSH
tunnel:

```bash
ssh -L 8088:127.0.0.1:8088 you@your-spark
```

### `nodes`

*Array. This is the important one.* One entry per machine. Everything in the UI — the
cards, every chart series, the thermal comparison, the cost breakdown, the topology
diagram — is generated from this list.

| Field | Meaning |
|---|---|
| `host` | Address the dashboard reaches this node at over SSH. **`null` (or omitted) means "the machine running the dashboard"** — no SSH involved. Exactly one node should normally have `null`. |
| `name` | Display name. Defaults to `host`, or the machine's hostname. Use whatever you call the box. |
| `id` | Stable key used inside the history file. Defaults to `node1`, `node2`, … **Don't change an existing id** or that node's history detaches from it. |
| `role` | `"head"` or `"worker"`. Cosmetic apart from marking rank 0 in the diagram. The first node becomes head if you don't say. |
| `rail1`, `rail2` | Optional. Fabric addresses used *only* to draw a node that is currently unreachable. Live rails are detected automatically; you almost never need these. |

Shorthands, all valid:

```json
"nodes": ["10.0.0.12", "10.0.0.13"]
"nodes": [{ "host": "10.0.0.12" }]
```

An unreachable node shows as "unreachable" on its own card and never blocks or slows
the others. Adding a node needs no code change; see [MULTI-NODE.md](MULTI-NODE.md).

### `ssh_user`

*String or null, default `null`.* Username for SSH to remote nodes. `null` uses whoever
runs the dashboard, which is right when the account name is the same everywhere.

### `ssh_options`

*Array of strings.* Default:

```json
["-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=accept-new"]
```

`BatchMode=yes` matters: it makes SSH fail immediately instead of hanging on a password
prompt nobody can answer. Raise `ConnectTimeout` if a node is across a slow link.

### `serve_ports`

*Array of integers, default `[8000, 8080, 8888]`.* Ports that might have an inference
engine on them. Each is probed on each node; one that answers `/v1/models` is treated as
a live engine, and its Prometheus `/metrics` is read for throughput.

List only ports you actually use — each unused one costs a probe per poll.

Common defaults: vLLM `8000`, llama.cpp `8080`, SGLang `30000`, Ollama `11434`.

### `router_port`

*Integer or null, default `null`.* If you run a proxy or router in front of several
engines on one endpoint, put its port here. It gets asked first for the model list,
because it knows about models the individual engines can't see.

### `fabric_prefix`

*String or null, default `null`.* You normally leave this alone.

Fabric rails — the high-speed node-to-node links — are detected from RoCE/InfiniBand
sysfs, so a cabled cluster is recognised with no configuration at all. Set this to an
IPv4 prefix like `"10.10."` only if your interconnect is plain Ethernet that wouldn't
otherwise be spotted.

Be specific. `"10."` would match most of a corporate LAN and group unrelated machines
into one fabric.

### `engine_patterns`

*Array of strings, default `["vllm", "llama", "sglang", "tgi", "text-generation"]`.*
Case-insensitive substrings matched against container names and process names to work
out engine uptime. Add yours if it isn't listed.

### `power`

*Object.* Starting values for the cost model:

```json
{ "price_kwh": 0.15, "currency": "$", "idle_w": 50, "load_w": 200 }
```

These are only the initial values — the ⚙ Settings form in the dashboard writes to
`settings.json` in your data directory, which then takes precedence. That is deliberate:
saving from the UI never rewrites the file you hand-edited.

`idle_w` and `load_w` are per node. **The DGX Spark has no wall-power sensor**, so cost
is modelled between these two points. Calibrating them with a smart plug is what makes
the numbers real: [METRICS.md](METRICS.md#power--cost).

### `data_dir`

*Path, default `~/.local/share/spark-monitor`.* Holds `history.jsonl`, `settings.json`
and the optional `catalog.json`.

Point it at a persistent disk if `$HOME` is on tmpfs. If you use the systemd unit and
change this, update `ReadWritePaths` in the unit too.

### `docs_dir`

*Path or null, default: this repository's `docs/` directory.* Markdown files offered in
the 📚 Docs drawer. Set to `null` to disable the drawer, or point it at your own runbook
directory to read your notes on your phone. Only `*.md` files directly inside it are
served.

### `history_days`

*Integer, default `45`.* How long samples are kept. Pruned once a day. At ~50 bytes per
sample, 45 days is roughly 3 MB — there is little reason to lower it, and 30-day trends
need at least 30.

### `sample_seconds`

*Integer, default `60`, minimum `10`.* How often the background sampler records a data
point. This is the only continuous work Spark Monitor does. Lowering it gives finer
charts and a bigger file; there is rarely a reason to.

### `catalog`

*Object.* Only used by `spark-catalog.py`; the dashboard ignores it.

| Key | Meaning |
|---|---|
| `scan` | Directories and globs searched on each node. `~` expands on the node. A directory counts as a model only if it contains `.safetensors`, `.bin` or `.gguf` files. |
| `shared_storage.mount` | Optional NAS/NFS path holding master copies. `null` disables the section. Asked of each node in turn, so one node being down doesn't blind it. |
| `shared_storage.sections` | Subdirectories to size up individually. |
| `pinned` | `{"substring": "reason"}`. Copies that are **required**, not waste — a model sharded across nodes with tensor parallelism must exist on each of them. Anything not pinned and present more than once is reported as reclaimable. |

## Validating a change

```bash
python3 spark-monitor.py --check
```

Reports the resolved paths and probes every node. A malformed JSON file is reported with
its line number instead of failing silently.
