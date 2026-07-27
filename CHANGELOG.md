# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Screenshots in the README and guides, also served to the in-dashboard docs drawer
  at `/docs-images/<name>`. The markdown renderer now handles `![alt](src)`.
- `contrib/keepalive.sh` for systems without systemd.

### Fixed
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
