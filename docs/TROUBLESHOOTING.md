# Troubleshooting

## Start here

```bash
python3 spark-monitor.py --check
```

This prints the config file actually in use, the resolved paths, and probes every node
with a pass/fail and a reason. Most problems are identified in that one screen.

If the service is behaving oddly rather than not starting:

```bash
systemctl --user status spark-monitor
journalctl --user -u spark-monitor -n 50 --no-pager
```

Note `--user` on both. Without it you're asking about a system service that doesn't
exist, and you'll be told the unit isn't found.

---

## The page won't load

**First, is the server up?** On the Spark itself:

```bash
curl -s http://127.0.0.1:8088/healthz
```

### `ok`, but you can't reach it from another device

The dashboard is fine; something in between isn't.

1. **Check `bind`.** If it's `127.0.0.1`, only local connections are accepted by design.
   Set it to `0.0.0.0` and restart, or use an SSH tunnel.
2. **Check the address.** Run `hostname -I` on the Spark and use the first address.
3. **Firewall.** `sudo ufw status` — if it's active, allow the port from your subnet
   (see [INSTALL.md](INSTALL.md#firewall)).
4. **Wrong network.** Guest Wi-Fi and VLANs commonly can't reach the main LAN.
5. **`http`, not `https`.** There's no TLS. Some browsers silently upgrade — type the
   `http://` prefix explicitly, and check for a stored HSTS entry if it keeps switching.

### Nothing on `127.0.0.1` either

```bash
journalctl --user -u spark-monitor -n 30 --no-pager
```

- **`cannot bind 0.0.0.0:8088: Address already in use`** — something else has the port.
  Find it with `ss -tlnp | grep 8088` and either stop it or change `port` in your config.
- **`cannot read config …: Expecting ',' delimiter: line 7 column 3`** — invalid JSON.
  The line number is exact. A trailing comma after the last item is the usual cause.
- **`cannot create data dir`** — a permissions problem, or `$HOME` isn't writable. Point
  `data_dir` somewhere you can write.
- **Nothing at all in the log** — the service may not be installed. `systemctl --user
  list-unit-files | grep spark` should show it as `enabled`.

---

## A node says "unreachable"

Almost always SSH. Test exactly what the dashboard does, from the node running it:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=3 10.0.0.12 nvidia-smi
```

| What you see | Fix |
|---|---|
| `Permission denied (publickey)` | Key isn't installed on that node: `ssh-copy-id you@10.0.0.12` |
| Hangs, then times out | Wrong address, node off, or the network is blocking it. `ping` it. |
| `Host key verification failed` | Connect once interactively to accept it: `ssh 10.0.0.12` |
| Asks for a passphrase | Your key has one. A background service can't answer — make a passphrase-less key for this, or use an agent with lingering. |
| Works, but `nvidia-smi: command not found` | Not a GPU node, or drivers aren't installed there. |
| Works fine by hand | Different environment. The service runs as your user with a minimal environment — check the username matches (`ssh_user`) and the key is at a default path or named in `~/.ssh/config`. |

If the address changed because of DHCP, give the node a reservation or use a
[Tailscale](TAILSCALE.md) address, which never moves.

---

## The model shows as down but it's running

The dashboard looks for an OpenAI-compatible `/v1/models` on each port in `serve_ports`.

**Check what it's checking**, on the node running the engine:

```bash
curl -s http://127.0.0.1:8000/v1/models
```

- **Nothing on that port** → add the real port to `serve_ports` in your config.
- **Engine bound to `127.0.0.1` on a *remote* node** → that's fine. The probe runs *on*
  that node over SSH, so loopback is reached correctly.
- **Engine needs an API key** → the probe sends none. Serve on a trusted network without
  auth, or accept that model detection won't work.
- **Not OpenAI-compatible** → the node card and thermals still work; the model panel
  won't.

## Throughput and totals are blank, but the model is up

Your engine isn't exposing Prometheus metrics.

**vLLM** exposes `/metrics` by default:

```bash
curl -s http://127.0.0.1:8000/metrics | head
```

**llama.cpp only serves `/metrics` when started with `--metrics`.** Without it, Spark
Monitor falls back to `/props` and `/slots`, which is enough for live rates but has no
lifetime counters. The panel says so rather than leaving suspicious blanks. Add
`--metrics` to your `llama-server` command line to get the totals and prefix-cache stats.

## Throughput reads 0 while requests are in flight

Normal during prefill. A long prompt can be processed for many seconds before the first
token is generated, and throughput measures generation. Watch the prompt (prefill) line
on the throughput chart — that's where the work is.

---

## Charts say "collecting first samples"

The sampler runs every 60 seconds and charts need at least two points. Wait a couple of
minutes.

If it persists past five minutes:

```bash
wc -l ~/.local/share/spark-monitor/history.jsonl
```

Not growing means the sampler thread isn't writing — check the log, and check the
directory is writable. If you're using the systemd unit with a custom `data_dir`, update
`ReadWritePaths` in the unit to match.

## Trends look wrong after a restart

Expected and handled. Engine counters reset to zero on restart, which would otherwise
appear as a huge negative rate. Spark Monitor detects the reset, discards the negative
delta, and records a `restart` event you'll see in **Cluster events**.

## History gap

A `gap` event means no samples were recorded — the dashboard or its host was down. The
duration is in the event text.

---

## Cost figures look wrong

They're an estimate, and if you haven't calibrated them they're built on generic
defaults. **The DGX Spark has no wall-power sensor** — this can't be measured, only
modelled. Ten minutes with a smart plug fixes it:
[METRICS.md](METRICS.md#calibrating-it-10-minutes-makes-it-real).

If the *first* day looks extreme, note that a partial day is extrapolated to a full-day
rate — the UI labels this. It settles after 24 hours.

## Temperatures look alarming

Read [METRICS.md](METRICS.md#temperatures) before worrying. The **SoC hotspot** number
reads far above GPU temperature by design, and 80–90 °C there is routine. What matters is
the trend, and the gap between nodes.

---

## The docs drawer is empty

It reads `*.md` from `docs_dir`, which defaults to this repository's `docs/` directory. If
you moved or symlinked the script away from the repo, set `docs_dir` explicitly. Setting
it to `null` disables the drawer.

## Model Catalog says "no catalog yet"

Expected — the catalog is optional. Generate one:

```bash
./spark-catalog.py --write
```

It's a point-in-time snapshot, not live; re-run it after moving models around. If it
finds nothing, check `catalog.scan` in your config — a directory only counts as a model
if it contains `.safetensors`, `.bin` or `.gguf` files.

## The dashboard feels slow

The first `/api/stats` after the 3-second cache expires does the real work: on a
multi-node cluster that's SSH round trips to every node, typically a few seconds. Cached
responses are sub-millisecond.

To speed up the cold path:

- **Trim `serve_ports`** to the ports you actually use — each unused one is a probe per
  node per poll.
- **Remove nodes that are switched off.** An offline node costs its full `ConnectTimeout`.
- **Check the network path** to any node on Wi-Fi or a slow link.

---

## Still stuck?

Open an issue with:

```bash
python3 spark-monitor.py --check 2>&1
journalctl --user -u spark-monitor -n 50 --no-pager
python3 --version && uname -a
```

**Redact your IP addresses and hostnames** before posting — `--check` prints them.
