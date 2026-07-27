# Architecture

Read this before changing the code. It explains not just how it works, but why several
obvious-looking improvements are deliberately not made.

## The whole thing

```
spark-monitor.py          the server: ~1700 lines, standard library only
  ├── config             load / merge / normalize a JSON config
  ├── collectors         node_stats(), group_metrics() — shell out, parse
  ├── topology           topo_groups() — derive the fabric graph from live rails
  ├── history            sampler thread + history() aggregation
  ├── HTML               one string: CSS, SVG charts and JS, no framework
  └── H(BaseHTTPRequestHandler)   the routes

spark-catalog.py          optional model inventory; imports the config loader
assets/*.woff2            self-hosted fonts
contrib/*.service         systemd unit template
```

There is no build step, no bundler, no dependency file, and no generated code. What you
edit is what runs.

## Why one file with no dependencies

This is the central constraint, and the reason the tool is dependable.

A monitoring tool has to work when other things are broken. Every dependency is something
that can fail to install on ARM, publish a breaking major version, or need patching. A
file that imports only the standard library keeps working across OS upgrades and Python
point releases with no maintenance at all.

It also means a hostile-looking install is auditable in an afternoon: `git clone`, read
the file, run it. Nothing is fetched at runtime — no CDN for fonts or scripts, so the
dashboard renders identically on a machine with no internet access.

**If you're adding a dependency, you're solving the problem the wrong way.** See
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Poll-on-access

Live metrics are collected only when a browser asks, with a 3-second cache absorbing
bursts (a page open in three tabs is one collection).

The alternative — a background loop polling everything continuously — means a dashboard
nobody is watching permanently consumes CPU on machines whose entire purpose is
inference. Poll-on-access makes an idle dashboard genuinely free.

**The one exception** is the 60-second sampler thread. Trends need continuous data;
there's no way around it. It's kept as cheap as possible: a handful of reads per node,
about 50 bytes per line appended to `history.jsonl`, pruned to 45 days. A full 45 days is
a few megabytes.

## Collection: SSH, not agents

Remote node metrics come from short read-only commands over SSH, run in parallel threads,
with the local node's collection running alongside them.

Trade-offs, stated honestly:

- **For:** nothing to install or update on the other nodes, no open ports, no agent to
  crash, and it uses the keys you already have.
- **Against:** SSH connection setup costs ~100 ms per command, so a cold poll on a
  multi-node cluster takes a few seconds.

The choice reflects the target: a handful of machines on a desk, not a datacentre. At
fifty nodes this would be the wrong design.

Collectors are written to **degrade rather than fail**. `sh()` returns `""` on any error
or timeout, every parse is guarded, and a node that produces nothing is marked
`online: false`. A node being unplugged mid-poll shows one card as unreachable and
affects nothing else.

## Topology is derived, never declared

`topo_groups()` builds the cluster graph fresh on every poll:

1. Each node reports which of its interfaces are backed by an RoCE/InfiniBand device, by
   reading `/sys/class/infiniband/*/device/net/*`, and which IPv4 addresses sit on them.
2. Nodes sharing a fabric subnet are adjacent.
3. Connected components of that graph are the fabric groups; each group's shape (`solo`,
   `direct`, `ring`, `mesh`) comes from its degree sequence.

This is why adding a node needs no topology configuration, and why **re-cabling shows up
on the next poll**. A declared topology drifts out of date the moment hardware moves,
and then actively misleads you. A derived one can't.

Group membership also scopes metrics: each group's throughput is gathered only from
engines on its own nodes, so two independent groups aren't summed into one number that
describes nothing.

`rail1`/`rail2` in the config are a fallback used *only* to place a node that's currently
unreachable, so a node that's merely off still draws in its last known position instead
of vanishing.

## Rates from cumulative counters

Engines expose lifetime counters, not rates. Rates are computed from deltas between
polls, which needs care:

- **Restarts reset counters.** A negative delta is treated as a reset — discarded, and
  recorded as a `restart` event rather than charted as a spike.
- **First sample has no rate.** Reported as `null`/`sampling…`, not as zero.
- **llama.cpp has no lifetime counter** unless started with `--metrics`. The fallback
  integrates per-slot decode progress between polls and sets `partial: true`, so the UI
  can say "live figures only" instead of showing a since-boot subtotal as if it were the
  lifetime total.

Prompt and generated tokens are tracked **separately**, never summed. Summing them is the
obvious thing to do and it destroys the most useful signal in the dataset — see
[METRICS.md](METRICS.md#generated-vs-prompt-tokens--read-this-one).

## Frontend: no framework, SVG charts

The UI is one HTML string with inline CSS and JS. Charts are SVG paths generated in
JavaScript from the `/api/history` arrays. No charting library, no React, no build.

Why: the whole page is ~48 KB and needs no network beyond the two API calls, so it loads
instantly over a VPN on a phone and works with no internet. A charting library would be
larger than the entire application.

Design system: CSS custom properties with a `[data-theme=light]` override, so both themes
come from one set of variables. Theme is applied before first paint from `localStorage` to
avoid a flash. Node series colours are `--n1`..`--n4`, cycled by index.

The page installs as a PWA (manifest + generated icon), which is how it becomes a phone
app with no App Store, no signing and no update mechanism — it's just the page.

## History storage: append-only JSONL

`history.jsonl`, one compact JSON object per sample, keys shortened (`g` for GPU util,
`tc` for SoC temp). Append-only: a crash costs at most one line.

Pruning rewrites the file once a day. `_norm()` upgrades rows written by older versions,
so history survives updates.

No database, because SQLite would add a schema to migrate and a file to corrupt in
exchange for query features this workload doesn't need. Aggregation over 45 days of
minute samples (~65 k rows) takes milliseconds, and `/api/history` is cached for 55
seconds.

## Security posture

**No authentication, by design, for a LAN/tailnet tool.** Adding auth to a dashboard you
open on your phone means a login flow, session storage, and a password to lose — for a
tool whose threat model is "my home network". [Tailscale](TAILSCALE.md) solves the remote
case properly, and an SSH tunnel plus `bind: 127.0.0.1` solves the paranoid case.

What that posture demands in return:

- **Read-only, with exactly one exception.** `POST /api/settings` takes four numeric and
  string fields, all clamped, and writes only its own settings file. There is no endpoint
  that can restart an engine or touch a node.
- **No remote power control.** Deliberately absent. A remote `poweroff` on a machine with
  no BMC strands it — you cannot power it back on. The feature is a footgun with no
  recovery path.
- **Path handling.** `/docs/` resolves names against a dictionary the server builds
  itself; no path from a URL is ever joined onto a directory. `/assets/` serves only
  whitelisted filenames.
- **Command construction.** Node addresses come from your own config, not from requests.
  No HTTP input reaches a shell.

Full detail: [SECURITY.md](../SECURITY.md).

## Deliberately not done

Things that look like obvious wins and aren't. Please don't send these.

| Not doing | Why |
|---|---|
| React / Node / WebSocket stack | Trades zero-dependency reliability and instant loads for a push cadence nobody needs at 5-second refresh |
| Continuous background polling | Makes an unwatched dashboard cost real CPU on inference machines |
| SQLite or a time-series DB | A schema to migrate and a file to corrupt, for query features this workload doesn't need |
| Wake-on-LAN / remote shutdown | Strands a node with no BMC, with no way to recover |
| Auth, users, sessions | Wrong shape for a LAN tool; Tailscale and SSH tunnels solve it better |
| Storing SSH passwords | Keys only. Nothing secret is stored, so nothing secret can leak |
| Log scraping for activity | Tried and reverted: engines don't log per-request lines by default, formats differ, and UTC logs mixed with local UI made quiet nights look like outages |
| Bundling a charting library | Bigger than the whole application |

## Worth adding

If you want to contribute something substantial:

- **Memory-bandwidth sampling** via `nvidia-smi dmon` — confirmed available on this
  hardware and genuinely useful on unified memory.
- **Network and storage I/O rates** per node, from `/proc/net/dev` and `/proc/diskstats`.
- **Per-model attribution** when several models share a node.
- **A CSV or JSON export** of a history window.

## Testing changes

There's no test suite — it's a single file whose behaviour is almost entirely "does this
shell command parse on real hardware", which mocks can't tell you.

What to do instead:

```bash
python3 -m py_compile spark-monitor.py     # syntax
python3 spark-monitor.py --check           # probes every node for real
python3 spark-monitor.py --port 18099 --bind 127.0.0.1   # run alongside the real one
```

Then exercise every endpoint and confirm the UI renders in both themes, at phone and
desktop widths, and with a node deliberately switched off. Verify on real hardware before
sending a pull request, and say in the PR what you verified on.
