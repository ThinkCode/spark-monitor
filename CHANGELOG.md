# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Optional `historian` config block: each sample is also pushed as Influx line protocol
  (stdlib `urllib` only) to a long-term store such as VictoriaMetrics, through a capped
  on-disk retry spool that drains when the target is reachable again. Off by default;
  `history.jsonl` is unchanged. Series and queries in `docs/API.md`.
- `contrib/macmini-historian/`: VictoriaMetrics on a Mac mini under launchd, 1-year
  retention, optional scrape of vLLM `/metrics`.
- **Firmware and versions drawer.** Reports BIOS/UEFI, embedded controller, NIC and
  NVMe firmware, the NVIDIA driver, the kernel, and pending driver/kernel packages for
  every node. The button glows with a count when something is behind. Read-only: it
  generates the root commands to apply an update and stops there, because a failed UEFI
  or embedded-controller capsule can leave a machine that will not boot and no remote
  orchestration recovers that. See `docs/FIRMWARE.md`, which includes measured capsule
  reboot times (12 minutes per node, not the 1-2 a kernel reboot takes).
- **BIOS drift detection.** No vendor publishes a machine-readable "latest BIOS" feed,
  so the cluster is its own reference: among nodes reporting the same product, one
  behind the newest BIOS observed is flagged. Reported as *drift*, not as an available
  update — "one node is behind another" is all this can honestly know.
- `firmware_report` config key: an optional JSON file of versions gathered by something
  the dashboard cannot reach itself, rendered with its own staleness marker. Read only.
- `AGENTS.md`: setup instructions written for a coding agent, including the constraints
  it must not break (standard library only, one write endpoint, no real addresses).
- **Topology switching** (`topology.py`, off by default). Declare profiles of what each
  node should run; the switcher diffs current against target and moves the minimum.
  Works as a CLI on its own, or from the dashboard when you enable control. Includes
  maintenance mode, which frees named nodes for firmware work and stops there.
- **Gated control endpoints.** Off unless you set `control.enabled` and a token of at
  least 16 characters, and refuse to enable otherwise. The token goes in an
  `X-Spark-Token` header, never a form field or query parameter: this dashboard has no
  login, so a cross-origin form POST from any page you visit would otherwise be able to
  switch your cluster. A custom header cannot be set cross-origin, so it is both the
  authentication and the CSRF defence. Plus a network allowlist defaulting to loopback
  and the Tailscale range, and constant-time token comparison.
- **`estop-agent.py`**, for pausing scheduled work during a switch when the scheduler
  runs somewhere the dashboard cannot reach. It acknowledges, and the dashboard waits
  for that before stopping anything — without the handshake a 5 s poll is a 5 s window
  where a cron job fires at a model that is going away. If the agent is absent, a switch
  from the UI aborts rather than proceeding silently unpaused; the CLI is unaffected and
  the UI offers an explicit override.
- **`docs/FIELD-NOTES.md`**: the failures already paid for on a live cluster — how to
  tell a hung distributed start from a slow one, why a capsule reboot is 12 minutes when
  uptime claims 5, `RemainAfterExit` making `systemctl start` a no-op, a hardened
  systemd unit stripping `ping`, block-buffered output making long jobs look hung, and
  others.
- Screenshots in the README and guides, also served to the in-dashboard docs drawer
  at `/docs-images/<name>`. The markdown renderer now handles `![alt](src)`.
- `contrib/keepalive.sh` for systems without systemd.

### Fixed
- The docs drawer's emphasis handling ran across code-span contents, so an asterisk
  inside a code span paired with a real italic marker later in the line and corrupted
  both. Code spans are now opaque to emphasis, as markdown specifies.
- Blockquotes were rendered line by line, so `**bold**` wrapping across lines broke and
  fenced code inside a quote was ignored. They are now collected and rendered
  recursively.
- `contrib/keepalive.sh` matched on the filename alone, so `vim spark-monitor.py`
  counted as a running server: editing the file while it was down meant the keepalive
  would never restart it. It now matches the interpreter too.
- The cron keepalive documented in INSTALL.md was a silent no-op: `pgrep -f` matched
  the cron shell's own command line, so it always believed the dashboard was running
  and never restarted it. Replaced with `contrib/keepalive.sh`, whose own command line
  does not contain the pattern. Verified: the old one-liner started no process at all;
  the script starts one, refuses to start a second, and recovers after a kill.
- The docs drawer's markdown renderer mangled several standard constructs: `*italic*`
  printed literal asterisks, `*italic*` nested inside `**bold**` broke both, list items
  wrapped across source lines were split into separate bullets, and paragraphs wrapped
  across lines broke any `**bold**` or `[link]` spanning the break.
- A node that was switched off cost the full SSH connect timeout on every probe,
  serially, adding roughly 20 s to a cold poll. A single liveness check now fails fast.

## [1.0.0] — 2026-07-27

First public release.

### Added
- Single-file, dependency-free dashboard server (`spark-monitor.py`).
- Per-node live metrics: GPU utilization/temperature/power, CPU load, unified memory,
  storage, SoC hotspot and NVMe temperatures, uptime, containers, fabric bytes sent.
- Model and throughput panel per topology group: live models, tokens/sec, requests in
  flight and queued, KV-cache usage, prefix-cache hit rate, memory headroom, engine
  uptime. Supports vLLM and llama.cpp, including llama.cpp servers started without
  `--metrics`.
- Topology derived from live RoCE/InfiniBand interface data, drawn as `solo`, `direct`,
  `ring`, `mesh` or `switch`. Adding a node needs no topology configuration.
- Trends over 24 h / 7 d / 30 d: throughput split into generated and prompt tokens, GPU
  utilization, memory and KV cache, and a day×hour busy-times heatmap.
- Usage sessions and derived cluster events (engine restarts, monitoring gaps, high
  concurrency, thermal peaks), all in the viewer's local time.
- Thermal summary with an automatic placement hint when nodes differ by ≥8 °C on the SoC
  hotspot — which is usually airflow rather than a fault.
- Estimated power and cost per day/month/year and per node, calibratable from the UI.
  Clearly labelled as an estimate: the DGX Spark exposes no wall-power sensor.
- Alarm banner for conditions that need attention.
- In-dashboard documentation drawer.
- Optional model inventory (`spark-catalog.py`) with a drawer in the dashboard,
  distinguishing required copies from reclaimable duplicates.
- JSON API: `/api/stats`, `/api/history`, `/api/settings`, `/api/catalog`, `/api/docs`,
  `/healthz`.
- Installable as a PWA on iOS and Android; light and dark themes.
- `install.sh` installing a systemd user service (no root required), and
  `--check` for validating configuration and node reachability.

[Unreleased]: https://github.com/ThinkCode/spark-monitor/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ThinkCode/spark-monitor/releases/tag/v1.0.0
