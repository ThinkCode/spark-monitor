# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
