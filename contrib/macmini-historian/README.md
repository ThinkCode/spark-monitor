# Mac mini historian (VictoriaMetrics)

Spark Monitor keeps 45 days of samples on the Spark. To keep a year — and query it from
a phone, Grafana or anything Prometheus-compatible — run
[VictoriaMetrics](https://docs.victoriametrics.com/) single-node on an always-on Mac
mini. It is one binary, uses a few hundred MB of RAM at most, and accepts the Influx
line protocol Spark Monitor pushes.

Nothing is installed on the Sparks. The Mac mini may sleep: Spark Monitor spools
samples on disk and sends the backlog when it is reachable again.

## Install

```bash
mkdir -p ~/historian/bin ~/historian/data
# Download the darwin-arm64 victoria-metrics-*.tar.gz from
# https://github.com/VictoriaMetrics/VictoriaMetrics/releases, then:
tar -xzf victoria-metrics-darwin-arm64-*.tar.gz -C ~/historian/bin
cp scrape.example.yml ~/historian/scrape.yml        # edit targets, or remove the flag
sed "s|__HOME__|$HOME|g" com.spark-monitor.victoriametrics.plist \
  > ~/Library/LaunchAgents/com.spark-monitor.victoriametrics.plist
launchctl load -w ~/Library/LaunchAgents/com.spark-monitor.victoriametrics.plist
curl -s localhost:8428/health                        # -> OK
```

Data lives in `~/historian/data`, retention is 1 year (`-retentionPeriod=1y`). A
year of Spark Monitor samples for a few nodes is well under 100 MB.

## Point Spark Monitor at it

In the Spark's `~/.config/spark-monitor/config.json`:

```json
"historian": { "url": "http://mac-mini.local:8428", "format": "influx-line" }
```

Restart the service; the startup banner prints `historian  http://…`. Use the
Tailscale name instead of `.local` if the Sparks and the mini aren't on one LAN.
Within a minute:

```bash
curl -s 'http://mac-mini.local:8428/api/v1/query?query=spark_gpu_util'
```

Series and query examples: [docs/API.md](../../docs/API.md#long-term-history-victoriametrics).

## Optional: scrape vLLM directly

`scrape.yml` makes VictoriaMetrics pull each engine's `/metrics` every 30 s, keeping
every `vllm:*` series rather than the handful Spark Monitor summarises. Delete the
`-promscrape.config` line from the plist if you don't want it.

## Security

Like Spark Monitor, VictoriaMetrics has no authentication by default. Keep port 8428 on
your LAN or tailnet; don't port-forward it. Add `-httpAuth.username`/`-httpAuth.password`
if the mini sits on a shared network (the Spark-side push does not send credentials yet).

## Uninstall

```bash
launchctl unload -w ~/Library/LaunchAgents/com.spark-monitor.victoriametrics.plist
rm ~/Library/LaunchAgents/com.spark-monitor.victoriametrics.plist
rm -rf ~/historian
```
