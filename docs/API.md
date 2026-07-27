# JSON API

Everything the dashboard displays comes from these endpoints. They're plain JSON over
HTTP with no authentication, so anything that can reach the port can read them — which
makes building on top of Spark Monitor about as easy as it gets.

`/api/stats` is treated as a stable contract: fields get added, not removed or renamed.

```bash
curl -s http://your-spark:8088/api/stats | jq .
```

---

## `GET /healthz`

Returns `ok` as plain text. Cheap, does no collection at all. Use it for uptime checks
and readiness probes rather than `/api/stats`, which does real work.

## `GET /api/stats`

Live state of every node and engine.

**Poll-on-access with a 3-second cache.** The first request after the cache expires
actually goes and collects — on a multi-node cluster that means SSH round trips, so
expect a few seconds. Requests inside the window are served from cache in under a
millisecond. Don't poll faster than the cache; you'll get identical data.

```json
{
  "ts": 1785136896.4,
  "cluster_name": "Home Lab",
  "topology": "direct",
  "registry": [
    { "id": "n1", "name": "spark-1", "role": "head", "rails": ["10.10.7.1"] }
  ],
  "nodes": {
    "n1": {
      "online": true,
      "gpu_util": 96, "gpu_temp": 64, "gpu_power_w": 54.6,
      "load1": 3.2, "cores": 20, "cpu_pct": 16.0,
      "mem_used_gb": 116.3, "mem_total_gb": 119.7, "mem_pct": 96.9,
      "disk_used_gb": 812.0, "disk_total_tb": 3.58, "disk_pct": 22.1,
      "soc_temp": 90, "soc_zones": [90, 88, 71, 66],
      "nvme_temp": 56,
      "uptime": "5 days, 2 hours",
      "containers": ["vllm-server"],
      "fabric_tx_tb": 91.74,
      "fabric_ifs": ["enp1s0f0np0", "enp1s0f1np1"],
      "rails": [{ "if": "enp1s0f0np0", "ip": "10.10.7.1", "plen": 24 }],
      "serving": [8000]
    }
  },
  "groups": [
    {
      "id": "fabric-n1", "kind": "direct",
      "node_ids": ["n1", "n2"], "head": "n1",
      "endpoints": ["http://127.0.0.1:8000"],
      "links": [{ "subnet": "10.10.7.0/24", "from": "n1", "to": "n2", "rails": 2 }],
      "rail_count": 2,
      "metrics": {
        "models": ["my-model"], "model_up": true, "max_ctx": 1048576,
        "engine_kind": "vLLM", "partial_metrics": false, "slots": null,
        "tok_s": 42.5, "req_running": 6, "req_waiting": 9,
        "kv": 12.8, "endpoints": 1,
        "gen_tokens": 4591003, "req_done": 10008,
        "engine_name": "vllm-server", "engine_uptime": "3 days",
        "prefix_hit": 99.3
      }
    }
  ],
  "extras": {
    "model_up": true,
    "models": ["my-model"], "model": "my-model", "max_ctx": 1048576,
    "clients": ["100.64.1.9"], "client_count": 1,
    "agent_local": true,
    "tailscale": "spark-1"
  }
}
```

### Notes on the shape

**`nodes` is keyed by node `id`**, not by name — ids are stable, names are for display.
An unreachable node has `{"online": false}` and little else; always check `online`
first.

**`groups` is the interesting part.** Each entry is a set of nodes connected by the
fabric, derived from live interface data on every poll. `kind` is one of `solo`,
`direct`, `ring`, `mesh` or `switch`. Metrics are attributed **per group**, gathered only
from engines on that group's own nodes — so two independent groups aren't summed into one
meaningless total.

**`metrics.gen_tokens` is `null` when `partial_metrics` is true.** llama.cpp keeps no
lifetime counters unless started with `--metrics`, and reporting a since-boot subtotal as
if it were the lifetime figure would be a lie. Live rates are still accurate.

**`extras.tailscale`** is the local Tailscale hostname, or `null` if Tailscale isn't
installed.

## `GET /api/history?h=N`

Aggregated history for the last `N` hours. `h` is clamped to 1–720; the UI uses 24, 168
and 720. Cached for 55 seconds.

Series are bucketed — 120 buckets for `h ≤ 24`, otherwise 168 — so the response size
doesn't depend on the range. `t0` is the window start (epoch seconds) and `w` the bucket
width in seconds, so bucket `i` covers `t0 + i*w`.

| Field | Meaning |
|---|---|
| `t0`, `w`, `samples` | Window start, bucket width, total samples on file |
| `ids`, `names` | Node ids in series order, and their display names |
| `g`, `tg`, `tc`, `m` | Per node `{id: [...]}`: GPU util %, GPU temp, SoC temp, memory % |
| `kv`, `watts` | Cluster-wide series: KV cache %, estimated watts |
| `tpm`, `ppm` | Generated and prompt tokens per minute — **kept separate on purpose** ([why](METRICS.md#generated-vs-prompt-tokens--read-this-one)) |
| `gen24`, `gen7`, `prompt24`, `tok24`, `tok7`, `req24` | Totals over 24 h / 7 d |
| `peakq` | Peak requests in flight + queued, 24 h |
| `kwh24`, `kwh7`, `day_kwh`, `cost_day`, `cost_mo`, `cost_yr`, `cost7` | Energy and **estimated** cost |
| `node_kwh24` | `{id: kWh}` — per-node 24 h energy |
| `watts_now`, `partial_day` | Latest estimated draw; whether daily figures are extrapolated |
| `therm` | `{id: {gpu_avg, gpu_max, soc_avg, soc_max, nvme_max}}` over 24 h |
| `heat` | `[7][24]` avg tokens/min by weekday × hour, 28 days |
| `busiest` | e.g. `"Wed 14:00"` |
| `sessions` | `[{start, end, mins, tok, ptok, peak}]`, newest first |
| `events` | `[{t, kind, bad, txt}]` — `restart`, `gap`, `load`, `therm` |
| `active_min`, `duty` | Active minutes and duty cycle % in the window |
| `conf` | The power settings the figures were computed with |

Every cost field is modelled, not measured — see
[METRICS.md](METRICS.md#power--cost).

## `GET /api/settings` · `POST /api/settings`

The power-cost settings. **The only write endpoint in the entire server.**

```bash
curl -s http://your-spark:8088/api/settings
# {"price_kwh": 0.15, "currency": "$", "idle_w": 50, "load_w": 200}

curl -s -X POST http://your-spark:8088/api/settings \
  -H "Content-Type: application/json" \
  -d '{"price_kwh": 0.28, "currency": "£", "idle_w": 48, "load_w": 205}'
```

Accepts those four fields only and returns the stored result. Everything is validated and
clamped: `price_kwh` to 0–10, `idle_w` to 0–1000, `load_w` to 1–2000 and forced above
`idle_w`, `currency` to 1–3 characters from a symbol charset. Unknown keys are ignored,
malformed bodies return 400 with the current settings unchanged, and bodies over 4 KB are
truncated. It writes only `settings.json` in your data directory.

There is no endpoint that can restart an engine, reboot a node, or change your cluster in
any way. That's deliberate — see [SECURITY.md](../SECURITY.md).

## `GET /api/catalog`

The optional model inventory from `spark-catalog.py`. Absent is normal:

```json
{ "available": false, "error": "no catalog yet — run ./spark-catalog.py --write" }
```

When present: `models[]` with `name`, `bytes`, `copies`, `locations[]`, `served_as[]`,
`on_shared`, `required_reason`, `redundant_bytes`; plus `nas` (shared storage state),
`nodes[]` and `reclaimable_bytes`.

## `GET /api/docs` · `GET /docs/<file>.md`

The docs drawer. `/api/docs` returns `[{file, title}]`; `/docs/<file>.md` returns raw
markdown as `text/plain`.

Only files listed by `/api/docs` can be fetched — names are resolved against that
dictionary rather than joined onto a path, so directory traversal isn't possible.

## `GET /` · `/manifest.json` · `/icon.png`

The dashboard page and its PWA bits. The HTML is fully self-contained: no CDN, no
external requests, works with no internet access. `/icon.png` is generated in-process.

Fonts are at `/assets/<name>.woff2`, served with a one-year immutable cache. Only the
whitelisted filenames resolve.

---

## Building on it

**A simple alarm poller:**

```bash
#!/usr/bin/env bash
S=$(curl -fsS --max-time 30 http://your-spark:8088/api/stats) || exit 1
echo "$S" | jq -r '
  .nodes | to_entries[] |
  select(.value.online == false or (.value.soc_temp // 0) >= 98) |
  "ALERT: \(.key) online=\(.value.online) soc=\(.value.soc_temp)"'
```

**Feeding Prometheus:** rather than scraping this API, point Prometheus at your inference
engine's own `/metrics` — that's where these numbers come from, and you avoid a
translation layer. Use this API for the things the engine doesn't know: node thermals,
topology, and cost.

**Rate limits:** none, but `/api/stats` does real work. Respect the 3-second cache;
`/healthz` is the right endpoint for anything polling frequently.
