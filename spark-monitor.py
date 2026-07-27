#!/usr/bin/env python3
"""Spark Monitor — a zero-dependency dashboard for NVIDIA DGX Spark clusters.

    python3 spark-monitor.py                     # run it, open http://<host>:8088
    python3 spark-monitor.py --config PATH       # use a specific config file
    python3 spark-monitor.py --write-config      # write a starter config and exit
    python3 spark-monitor.py --check             # validate config + reachability

Python standard library only: no pip install, no build step, no database, no
JavaScript framework. One file you can read end to end.

Live stats are POLL-ON-ACCESS (3 s cache), so an idle dashboard costs the
cluster nothing. A 60 s sampler thread is the one continuous task; it keeps the
history that trends, thermals and cost estimates are built from.

HTTP endpoints
    GET       /                     dashboard UI (installable as a PWA)
    GET       /api/stats            live JSON for every node and engine
    GET       /api/history?h=N      trends, thermals and cost, last N hours
    GET       /api/catalog          optional model inventory (spark-catalog.py)
    GET/POST  /api/settings         power-cost settings (validated + clamped)
    GET       /api/docs             doc index   ·   GET /docs/<file>.md  raw
    GET       /manifest.json, /icon.png         PWA bits

SECURITY: there is NO authentication. Run it on a trusted LAN or a Tailscale
tailnet only, and never port-forward its port on your router. See SECURITY.md.

Configuration lives in a JSON file (see CONFIGURATION.md) — nothing in this
file needs editing to add nodes, change ports or point at a different fabric.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import struct
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

APP_NAME = "Spark Monitor"
VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(HERE, "assets")
# Fonts are self-hosted so the dashboard renders correctly with no internet
# access (it is a LAN/tailnet tool). This whitelist doubles as the
# content-type table for /assets/ — a path not listed here is a 404.
ASSETS = {
    "barlow-400.woff2": "font/woff2", "barlow-500.woff2": "font/woff2",
    "barlow-700.woff2": "font/woff2", "barlowcond-400.woff2": "font/woff2",
    "barlowcond-600.woff2": "font/woff2", "ai-node-v2.webp": "image/webp",
}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CACHE_TTL = 3.0


# ---------------------------------------------------------------- config ----
def _xdg(var, fallback):
    return os.environ.get(var) or os.path.expanduser(fallback)


CONFIG_PATH_DEFAULT = os.path.join(
    _xdg("XDG_CONFIG_HOME", "~/.config"), "spark-monitor", "config.json")

# Every knob, with the value used when the key is absent. A missing config file
# is NOT an error: Spark Monitor writes this out on first run, pointed at the
# machine it is running on, so `python3 spark-monitor.py` works with zero setup.
# You then edit that file to add the rest of your nodes.
DEFAULTS = {
    "cluster_name": "Spark Cluster",
    "port": 8088,
    # 0.0.0.0 makes the dashboard reachable from your other devices, which is
    # the entire point of it. There is no auth, so keep the port off the public
    # internet — see SECURITY.md.
    "bind": "0.0.0.0",
    # One entry per machine. "host" is the address this dashboard reaches the
    # node at over SSH; null means "this machine" (no SSH hop). Everything in
    # the UI — cards, charts, thermal series, cost lines, the topology diagram
    # — is derived from this list, so adding a node is a config edit.
    "nodes": [],
    "ssh_user": None,            # null = the user this process runs as
    "ssh_options": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                    "-o", "StrictHostKeyChecking=accept-new"],
    # Ports an inference engine may listen on. Each is probed per node; a port
    # that answers /v1/models is a live engine.
    "serve_ports": [8000, 8080, 8888],
    # Optional single entry point in front of several engines (a proxy/router).
    # Asked first when listing models, because it knows the whole picture.
    "router_port": None,
    # Fabric rails (the high-speed node-to-node interconnect) are detected from
    # RoCE/InfiniBand sysfs, so a cabled cluster needs no configuration. Set
    # this to an IPv4 prefix like "10.10." to additionally treat plain
    # Ethernet addresses on that prefix as fabric.
    "fabric_prefix": None,
    # Container/process names that count as "the inference engine" when
    # reporting engine uptime.
    "engine_patterns": ["vllm", "llama", "sglang", "tgi", "text-generation"],
    "data_dir": os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"),
                             "spark-monitor"),
    # Markdown rendered in the in-dashboard docs drawer. Defaults to this
    # repository's own docs/ directory, so the drawer works out of the box.
    "docs_dir": os.path.join(HERE, "docs"),
    # Starting values for the power model, editable later in the UI.
    "power": {"price_kwh": 0.15, "currency": "$", "idle_w": 50, "load_w": 200},
    "history_days": 45,
    "sample_seconds": 60,
}


def default_nodes():
    """A single-node registry describing the machine we are running on.

    Used on first run so the dashboard is useful before any configuration.
    """
    name = os.uname().nodename.split(".")[0] or "node1"
    return [{"id": "node1", "name": name, "role": "head", "host": None}]


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if k not in base:
            continue                     # ignore unknown keys, never crash
        if isinstance(base[k], dict) and isinstance(v, dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = v
    return out


def normalize_nodes(nodes):
    """Fill in what the user left out: ids, display names, and one head node.

    A minimal entry is just {"host": "10.0.0.12"} — everything else is
    inferred, so the config file people have to write stays tiny.
    """
    out, seen = [], set()
    for i, n in enumerate(nodes or []):
        if isinstance(n, str):           # bare "10.0.0.12" is allowed too
            n = {"host": n}
        if not isinstance(n, dict):
            continue
        host = n.get("host") or None
        nid = str(n.get("id") or f"node{i + 1}")
        while nid in seen:               # ids index the history file: keep unique
            nid += "_"
        seen.add(nid)
        out.append({
            "id": nid,
            "name": str(n.get("name") or host or nid),
            "role": n.get("role") or ("head" if i == 0 else "worker"),
            "host": host,
            # Only used to draw a node that is currently unreachable; live rails
            # are read from the node itself on every poll.
            "rail1": n.get("rail1"), "rail2": n.get("rail2"),
        })
    if out and not any(n["role"] == "head" for n in out):
        out[0]["role"] = "head"
    return out


def load_config(path=None):
    """Read the config file, filling in DEFAULTS. Missing file is fine."""
    path = path or os.environ.get("SPARK_MONITOR_CONFIG") or CONFIG_PATH_DEFAULT
    raw, existed = {}, os.path.exists(path)
    if existed:
        try:
            with open(path) as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            sys.exit(f"{APP_NAME}: cannot read config {path}: {e}")
        if not isinstance(raw, dict):
            sys.exit(f"{APP_NAME}: config {path} must be a JSON object")
    cfg = _merge(DEFAULTS, raw)
    cfg["nodes"] = normalize_nodes(cfg["nodes"]) or default_nodes()
    cfg["data_dir"] = os.path.expanduser(cfg["data_dir"])
    if cfg.get("docs_dir"):
        cfg["docs_dir"] = os.path.expanduser(cfg["docs_dir"])
    cfg["_path"], cfg["_existed"] = path, existed
    return cfg


def write_starter_config(path):
    """Write a commented-by-example starter config for the local machine."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    starter = {
        "cluster_name": DEFAULTS["cluster_name"],
        "port": DEFAULTS["port"],
        "nodes": default_nodes(),
        "serve_ports": DEFAULTS["serve_ports"],
        "power": dict(DEFAULTS["power"]),
    }
    with open(path, "w") as f:
        json.dump(starter, f, indent=2)
        f.write("\n")
    return path


# Populated by main() (and by import, so the module stays usable in a REPL).
CFG = load_config()
NODES = CFG["nodes"]
PORT = CFG["port"]
SERVE_PORTS = tuple(CFG["serve_ports"])
ROUTER_PORT = CFG["router_port"]
FABRIC_PREFIX = CFG["fabric_prefix"]
DOCS_DIR = CFG["docs_dir"]
SAMPLE_EVERY = max(10, int(CFG["sample_seconds"]))
KEEP_DAYS = max(1, int(CFG["history_days"]))
HIST_FILE = os.path.join(CFG["data_dir"], "history.jsonl")
# Runtime settings the UI can change are kept apart from the user's config file
# so saving them never rewrites hand-edited configuration.
CONF_FILE = os.path.join(CFG["data_dir"], "settings.json")
CATALOG_FILE = os.path.join(CFG["data_dir"], "catalog.json")

# Power model. The DGX Spark exposes NO system power sensor (no INA/PMBus
# monitor, no /sys/class/power_supply, no hwmon power rail; nvidia-smi reports
# power.limit as N/A and its power.draw is GPU-domain only — not wall power).
# So wall draw is ESTIMATED from sampled GPU utilization between two
# calibratable endpoints. Measure yours with a smart plug and set them in the
# UI. See docs/METRICS.md.
DEFAULT_CONF = dict(CFG["power"])
_cache = {"t": 0, "data": None}
_tokrate = {}   # per-group: gid -> {"gt": prev generation_tokens_total, "t": ts}
_slotstate = {} # llama.cpp: url -> {slot_id: (id_task, n_decoded)}
_llama_total = {}  # llama.cpp: url -> tokens generated since this process started
_lock = threading.Lock()
_hcache = {}


def sh(cmd, timeout=6):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def conf():
    c = dict(DEFAULT_CONF)
    try:
        c.update({k: v for k, v in json.load(open(CONF_FILE)).items() if k in c})
    except Exception:
        pass
    return c


def save_conf(new):
    """Strictly validated: numeric fields clamped, currency limited."""
    c = conf()
    try:
        if "price_kwh" in new:
            c["price_kwh"] = round(min(10.0, max(0.0, float(new["price_kwh"]))), 4)
        if "idle_w" in new:
            c["idle_w"] = int(min(1000, max(0, float(new["idle_w"]))))
        if "load_w" in new:
            c["load_w"] = int(min(2000, max(1, float(new["load_w"]))))
        if "currency" in new:
            cur = str(new["currency"])[:3]
            c["currency"] = cur if re.fullmatch(r"[A-Za-z$€£¥₹]{1,3}", cur) else "$"
    except (TypeError, ValueError):
        return c
    if c["load_w"] <= c["idle_w"]:
        c["load_w"] = c["idle_w"] + 1
    try:
        os.makedirs(os.path.dirname(CONF_FILE), exist_ok=True)
        with open(CONF_FILE, "w") as f:
            json.dump(c, f, indent=2)
    except OSError:
        pass
    _hcache.clear()
    return c


def _ssh_prefix(host):
    """`ssh ... host` for a remote node, or "" for the local machine."""
    if not host:
        return ""
    opts = " ".join(str(o) for o in CFG["ssh_options"])
    target = f"{CFG['ssh_user']}@{host}" if CFG.get("ssh_user") else host
    return f"ssh {opts} {target}"


def _pfx(node):
    return _ssh_prefix(node.get("host"))


def _all_ports():
    """Every port a client could be talking to."""
    ports = list(SERVE_PORTS)
    if ROUTER_PORT and ROUTER_PORT not in ports:
        ports.append(ROUTER_PORT)
    return ports


# Fabric addresses learned from the nodes themselves. Rails are usually not in
# the config file (they are auto-detected), so they have to be remembered here
# for the "is this peer one of us?" test below.
_seen_rails = set()


def _is_internal(ip):
    """True when a peer is the cluster talking to itself rather than a client.

    Deliberately narrow: a workstation on the same LAN as the cluster IS a
    client and should be counted. Only loopback, link-local, container bridges,
    the fabric and the nodes' own addresses are excluded.
    """
    if ip.startswith(("127.", "::1", "169.254.")):
        return True                                    # loopback, link-local
    if re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
        return True                                    # container bridges
    if FABRIC_PREFIX and ip.startswith(FABRIC_PREFIX):
        return True
    if ip in _seen_rails:
        return True
    return any(ip == n.get(k) for n in NODES
               for k in ("host", "rail1", "rail2"))


def node_stats(node):
    pre = _pfx(node)

    def r(cmd, timeout=6):
        return sh(cmd if not pre else f"{pre} '{cmd}'", timeout)

    out = {}
    # Liveness first, with one cheap command.
    #
    # Without this, a node that is switched off costs the SSH connect timeout on
    # EVERY probe below — a dozen of them, serially — so one powered-down node
    # added ~20 s to a cold poll and made the whole dashboard feel broken.
    # Failing fast here keeps an offline node to a single timeout.
    if pre and r("echo up", timeout=5) != "up":
        return {"online": False, "unreachable": True}

    g = r("nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw --format=csv,noheader,nounits")
    if g:
        p = [x.strip() for x in g.split(",")]
        out["gpu_util"] = int(p[0]) if p[0].isdigit() else None
        out["gpu_temp"] = int(p[1]) if len(p) > 1 and p[1].isdigit() else None
        try:
            out["gpu_power_w"] = float(p[2])
        except Exception:
            out["gpu_power_w"] = None
    la, nc = r("cat /proc/loadavg"), r("nproc")
    if la:
        out["load1"] = float(la.split()[0])
        out["cores"] = int(nc or 20)
        out["cpu_pct"] = round(min(100.0, out["load1"] / out["cores"] * 100), 1)
    m = r('free -b | awk "NR==2{print \\$2, \\$3, \\$7}"')
    if m:
        t, u, _a = map(int, m.split())
        out["mem_total_gb"] = round(t / 2**30, 1)
        out["mem_used_gb"] = round(u / 2**30, 1)
        out["mem_pct"] = round(u / t * 100, 1)
    d = r('df -B1 / | awk "NR==2{print \\$2, \\$3}"')
    if d:
        t, u = map(int, d.split())
        out["disk_total_tb"] = round(t / 2**40, 2)
        out["disk_used_gb"] = round(u / 2**30, 0)
        out["disk_pct"] = round(u / t * 100, 1)
    # SoC hotspot = hottest ACPI thermal zone; NVMe from its own hwmon.
    zs = r('cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | tr "\\n" " "')
    vals = [int(x) // 1000 for x in zs.split() if x.isdigit()]
    if vals:
        out["soc_temp"] = max(vals)
        out["soc_zones"] = sorted(vals, reverse=True)[:4]
    # NVMe temperature. Find the hwmon whose name is "nvme" rather than
    # assuming an index — hwmon numbering is not stable across machines or
    # reboots (on one node nvme was hwmon1, with mlx5 and acpitz around it).
    nv = r('for h in /sys/class/hwmon/hwmon*; do '
           '[ "$(cat $h/name 2>/dev/null)" = nvme ] && '
           'cat $h/temp1_input 2>/dev/null && break; done')
    if nv.isdigit():
        out["nvme_temp"] = round(int(nv) / 1000)
    out["uptime"] = r("uptime -p").replace("up ", "")
    out["containers"] = [c for c in r('docker ps --format "{{.Names}}"').splitlines() if c]
    # Bytes shipped over the fabric, summed across every RoCE/IB port. The
    # counter is in 4-byte words. Ports are globbed, not named: device naming
    # differs between machines (rocep1s0f1, roceP2p1s0f1, mlx5_0, ...).
    rd = r('cat /sys/class/infiniband/*/ports/*/counters/port_xmit_data '
           '2>/dev/null | awk "{s+=\\$1} END{if(s>0)print s}"')
    if rd.isdigit():
        out["fabric_tx_tb"] = round(int(rd) * 4 / 2**40, 2)
    # Live fabric rails. The topology is DERIVED from these rather than
    # declared in config, so re-cabling shows up on the next poll instead of
    # leaving the dashboard describing a layout that no longer exists.
    #
    # A rail is an IPv4 address on a netdev backed by an RoCE/InfiniBand
    # device (/sys/class/infiniband/<dev>/device/net/<netdev>) — that is what
    # makes this work on any cluster with no configuration. `fabric_prefix`
    # additionally accepts plain Ethernet addresses on a chosen prefix.
    fabric_ifs = set(
        r('for d in /sys/class/infiniband/*/device/net/*; do '
          '[ -e "$d" ] && basename "$d"; done').split())
    out["fabric_ifs"] = sorted(fabric_ifs)
    out["rails"] = []
    for line in r('ip -o -4 addr show 2>/dev/null').splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        iface, cidr = f[1], f[3]
        if iface in fabric_ifs or (FABRIC_PREFIX and cidr.startswith(FABRIC_PREFIX)):
            ip, _, plen = cidr.partition("/")
            out["rails"].append({"if": iface, "ip": ip, "plen": int(plen or 24)})
            _seen_rails.add(ip)
    # Which serving ports actually answer on this node. Engines move between
    # ports as a cluster is reconfigured, so this is probed, never assumed.
    out["serving"] = []
    for p in SERVE_PORTS:
        if r(f"curl -fsS --max-time 2 -o /dev/null -w '%{{http_code}}' "
             f"http://127.0.0.1:{p}/v1/models 2>/dev/null", 6).strip() == "200":
            out["serving"].append(p)
    out["online"] = "mem_pct" in out
    return out


def collect():
    res, stats = {}, {"ts": time.time()}

    def one(n):
        res[n["id"]] = node_stats(n)

    def extras():
        e = {}
        # If a router/proxy is configured, ask it first — it is the single
        # source of truth for whatever is currently up. Otherwise (and if it
        # does not answer) probe the engine endpoints directly.
        names, maxctx = [], None
        rj = (sh(f"curl -fsS --max-time 4 http://127.0.0.1:{ROUTER_PORT}/v1/models")
              if ROUTER_PORT else "")
        try:
            names = [d["id"] for d in json.loads(rj)["data"]]
        except Exception:
            pass
        if not names or maxctx is None:
            # A router advertises model names but often not their context
            # length, so the engines are asked either way.
            found, maxctx = _models_at(model_endpoints())
            names = names or found
        e["model_up"] = bool(names)
        e["models"] = names
        e["model"] = ", ".join(names[:3]) if names else None
        e["max_ctx"] = maxctx
        # Who is actually being served: distinct peers holding an established
        # connection to a serving port. Split into two signals because they
        # answer different questions, and neither alone is enough:
        #   clients      — peers from outside the cluster (your laptop, phone)
        #   agent_local  — loopback/container traffic, i.e. something on this
        #                  box proxying for users who never appear as an IP
        #                  (a chat bot, a local agent). Those end users are
        #                  invisible at the network layer, so this flag is the
        #                  only hint they exist.
        # TCP connections also only exist *during* a request, which is why the
        # in-flight/queued counters below matter more than either of these.
        sports = " or ".join(f"sport = :{p}" for p in _all_ports())
        cl = sh(f"ss -tn state established '( {sports} )' "
                "| awk 'NR>1{split($4,a,\":\"); print a[1]}' | grep -v '^$'")
        peers = [c for c in cl.splitlines() if c]
        e["clients"] = sorted(set(c for c in peers if not _is_internal(c)))
        e["client_count"] = len(e["clients"])
        e["agent_local"] = any(c.startswith(("127.", "::1")) for c in peers)
        ts = sh("tailscale status --peers=false 2>/dev/null "
                "| head -1 | awk '{print $2}'")
        e["tailscale"] = ts or None
        # (watchdog/sync/global-engine facts were removed with the Cluster card;
        # engine uptime is now reported per topology group in group_metrics.)
        res["extras"] = e

    def grp(g):
        g["metrics"] = group_metrics(g)

    # Pass 1: per-node stats (also yields each node's live rails + serving ports).
    th = [threading.Thread(target=one, args=(n,)) for n in NODES]
    th.append(threading.Thread(target=extras))
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=12)
    nodes = {n["id"]: res.get(n["id"], {}) for n in NODES}

    # Pass 2: topology is derived from those rails, then each group's metrics are
    # scraped from the endpoints that actually live on its own nodes.
    groups = topo_groups(nodes)
    gth = [threading.Thread(target=grp, args=(g,)) for g in groups]
    for t in gth:
        t.start()
    for t in gth:
        t.join(timeout=12)

    # Remember which endpoints actually served, so the history sampler probes
    # those instead of the full node x port cross-product.
    live = [u for g in groups for u in g["endpoints"]]
    _live_endpoints[:] = live

    stats["cluster_name"] = CFG["cluster_name"]
    stats["nodes"] = nodes
    stats["extras"] = res.get("extras", {})
    stats["groups"] = groups
    stats["registry"] = [
        {"id": n["id"], "name": n["name"], "role": n.get("role"),
         "rails": [r["ip"] for r in (nodes.get(n["id"], {}) or {}).get("rails", [])]}
        for n in NODES]
    kinds = [g["kind"] for g in groups]
    stats["topology"] = ("ring" if "ring" in kinds else "mesh" if "mesh" in kinds
                         else "direct" if "direct" in kinds else "single")
    return stats


def get_stats():
    with _lock:
        if time.time() - _cache["t"] < CACHE_TTL and _cache["data"]:
            return _cache["data"]
        d = collect()
        _cache.update(t=time.time(), data=d)
        return d


# ---------- history sampling ----------
# Endpoints where an engine may be serving: every configured node crossed with
# every configured serving port. Once a poll has run, the set narrows to the
# endpoints that actually answered (`_live_endpoints`), so the 60 s sampler
# does not keep probing ports nothing listens on.
_live_endpoints = []


def model_endpoints():
    if _live_endpoints:
        return list(_live_endpoints)
    eps = []
    for n in NODES:
        base = n.get("host") or "127.0.0.1"
        for p in SERVE_PORTS:
            u = f"http://{base}:{p}"
            if u not in eps:
                eps.append(u)
    return eps


# vLLM renamed some series between releases; accept both spellings so the
# dashboard keeps working across engine upgrades instead of silently going
# blank. llama.cpp names are handled in _scrape_llamacpp.
_METRIC_PATS = (
    ("kv", ("kv_cache_usage_perc", "gpu_cache_usage_perc")),
    ("rr", ("num_requests_running", "requests_processing")),
    ("rw", ("num_requests_waiting", "requests_deferred")),
    ("pt", ("prompt_tokens_total",)),
    ("gt", ("generation_tokens_total", "tokens_predicted_total")),
    ("rs", ("request_success_total", "e2e_request_latency_seconds_count")),
    ("pq", ("prefix_cache_queries_total",)),
    ("ph", ("prefix_cache_hits_total",)),
)


def _scrape(url):
    sums, engine = {}, None
    mx = sh(f"curl -fsS --max-time 4 {url}/metrics")
    for line in mx.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith("vllm:"):
            engine = "vLLM"
        elif line.startswith("llamacpp:"):
            engine = "llama.cpp"
        else:
            continue
        name = line.split("{", 1)[0].split(None, 1)[0]
        for key, pats in _METRIC_PATS:
            if any(name.endswith(p) or name == "vllm:" + p for p in pats):
                try:
                    sums[key] = sums.get(key, 0.0) + float(line.rsplit(None, 1)[1])
                except Exception:
                    pass
                break
    if engine and sums:
        sums["engine"] = engine
    return sums


def _scrape_llamacpp(url):
    """llama.cpp fallback.

    llama.cpp only serves Prometheus /metrics when started with --metrics
    (/props reports endpoint_metrics), so a GGUF server otherwise reads as a
    live model with no numbers at all. /props + /slots are always on and carry
    enough to fill most of the panel. There is no lifetime token counter there,
    so throughput is integrated from per-slot decode progress between polls:
    per slot, if the task id is unchanged and n_decoded grew, that delta is
    real generation. Task changes are skipped rather than counted as a jump.
    """
    try:
        props = json.loads(sh(f"curl -fsS --max-time 4 {url}/props") or "{}")
        slots = json.loads(sh(f"curl -fsS --max-time 4 {url}/slots") or "[]")
    except Exception:
        return {}
    if not isinstance(slots, list) or not props:
        return {}

    prev = _slotstate.setdefault(url, {})
    gained, running, used, cap = 0, 0, 0, 0
    for s in slots:
        if not isinstance(s, dict):
            continue
        sid, task = s.get("id"), s.get("id_task")
        nt = (s.get("next_token") or [{}])
        dec = (nt[0] if isinstance(nt, list) and nt else {}).get("n_decoded") or 0
        p, d = prev.get(sid, (None, 0))
        if p == task:
            if dec >= d:                 # same task still generating
                gained += dec - d
        elif p is not None:
            # Slot moved to a new task since the last poll. n_decoded counts
            # from zero per task, so everything on the clock now is new work -
            # counting it (rather than skipping) is what makes short requests
            # that begin and end between polls show up at all.
            gained += dec
        prev[sid] = (task, dec)
        if s.get("is_processing"):
            running += 1
        ctx = s.get("n_ctx") or 0
        cap += ctx
        used += (s.get("n_prompt_tokens") or 0) + dec

    _llama_total[url] = _llama_total.get(url, 0) + gained
    out = {"rr": running, "rw": 0, "gt": _llama_total[url],
           "n_ctx": (slots[0].get("n_ctx") if slots else None) or 0,
           "slots": props.get("total_slots") or len(slots),
           "engine": "llama.cpp", "partial": True}
    if cap:
        out["kv"] = round(used / cap, 4)     # same 0..1 scale as vLLM's gauge
    return out


def _metrics_for(endpoints):
    """Aggregate engine metrics across a group's endpoints (vLLM or llama.cpp)."""
    total, live = {}, []
    lock = threading.Lock()

    meta = {}

    def one(u):
        s = _scrape(u)                       # Prometheus: vLLM or llama.cpp
        if s.get("engine") == "llama.cpp":
            # llama.cpp's metrics carry the lifetime counters but no cache-usage
            # gauge, so fold in /slots for KV, context and slot count. Metrics
            # win on any overlap, and the totals are now real (not partial).
            s = {**_scrape_llamacpp(u), **s, "partial": False}
        elif not s:
            s = _scrape_llamacpp(u)          # /metrics disabled: slots only
        if not s:
            return
        with lock:
            live.append(u)
            for k, v in s.items():
                # bool is a subclass of int - keep flags out of the numeric sums
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    meta[k] = v
                else:
                    total[k] = total.get(k, 0.0) + v

    th = [threading.Thread(target=one, args=(u,)) for u in endpoints]
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=8)
    out = {}
    if "kv" in total and live:
        # kv usage is a fraction per engine; average it rather than summing
        out["kv"] = round(total["kv"] / len(live) * 100, 1)
    for k in ("rr", "rw", "pt", "gt", "rs", "pq", "ph", "n_ctx", "slots"):
        if k in total:
            out[k] = int(total[k])
    out.update(meta)          # engine kind / partial flag (non-numeric)
    out["endpoints"] = len(live)
    return out


def _engine_metrics():
    return _metrics_for(model_endpoints())


# (serving endpoints are discovered per node in node_stats -> "serving" and
#  attached to their group by topo_groups)


def _models_at(endpoints):
    """Model names + max context advertised across a set of endpoints.

    Queried in parallel: a cluster can expose a dozen candidate endpoints and
    an unreachable one costs the curl timeout, which would otherwise add up to
    a visibly slow dashboard.
    """
    names, maxctx, lock = [], [None], threading.Lock()

    def one(u):
        try:
            data = json.loads(sh(f"curl -fsS --max-time 3 {u}/v1/models"))["data"]
        except Exception:
            return
        with lock:
            for d in data:
                if d["id"] not in names:
                    names.append(d["id"])
                maxctx[0] = maxctx[0] or d.get("max_model_len")

    th = [threading.Thread(target=one, args=(u,)) for u in endpoints]
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=6)
    return names, maxctx[0]


def _net(ip, plen):
    o = [int(x) for x in ip.split(".")]
    bits = (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]
    mask = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF
    n = bits & mask
    return f"{(n>>24)&255}.{(n>>16)&255}.{(n>>8)&255}.{n&255}/{plen}"


def topo_groups(node_stats_by_id):
    """Derive topology from the LIVE cabling rather than a hard-coded registry.

    Nodes sharing a fabric subnet are directly linked; each connected component
    of that graph is one fabric group (direct / ring / mesh by its shape), and a
    node with no rail is its own standalone group. Each group carries only the
    serving endpoints that live on its own nodes, so model metrics are attributed
    per topology instead of being summed across the cluster.
    """
    # subnet -> nodes on it  =>  adjacency
    subnets, adj, links = {}, {n["id"]: set() for n in NODES}, {}
    for n in NODES:
        st = node_stats_by_id.get(n["id"], {}) or {}
        rails = st.get("rails")
        if rails is None and not st.get("online"):
            # Unreachable: fall back to the registry so a node that is merely
            # down still draws in its last-known place instead of vanishing.
            rails = [{"if": "?", "ip": ip, "plen": 24}
                     for ip in (n.get("rail1"), n.get("rail2")) if ip]
        for r in rails or []:
            subnets.setdefault(_net(r["ip"], r["plen"]), []).append((n["id"], r))
    for net, members in subnets.items():
        for i, (a, ra) in enumerate(members):
            for b, rb in members[i + 1:]:
                adj[a].add(b)
                adj[b].add(a)
                links.setdefault(frozenset((a, b)), []).append(
                    {"subnet": net, "from": a, "from_if": ra["if"],
                     "to": b, "to_if": rb["if"]})
    # connected components
    seen, comps = set(), []
    for n in NODES:
        if n["id"] in seen:
            continue
        stack, comp = [n["id"]], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            stack.extend(adj[x] - seen)
        comps.append(comp)

    by_id = {n["id"]: n for n in NODES}
    order = {n["id"]: i for i, n in enumerate(NODES)}
    groups = []
    for comp in comps:
        comp.sort(key=lambda i: order[i])
        if len(comp) == 1:
            kind = "solo"
        elif len(comp) == 2:
            kind = "direct"
        else:
            deg = [len(adj[x]) for x in comp]
            edges = sum(deg) // 2
            kind = "ring" if all(d == 2 for d in deg) and edges == len(comp) else "mesh"
        head = next((i for i in comp if by_id[i].get("role") == "head"), comp[0])
        eps, seen_ep = [], set()
        for nid in comp:
            host = by_id[nid].get("host")
            for p in (node_stats_by_id.get(nid, {}) or {}).get("serving", []):
                u = f"http://{host or '127.0.0.1'}:{p}"
                if u not in seen_ep:
                    seen_ep.add(u)
                    eps.append(u)
        # one entry per node-pair, plus how many parallel rails that pair has
        comp_links = [dict(l[0], rails=len(l))
                      for k, l in links.items() if set(k) <= set(comp)]
        groups.append({
            "id": "fabric-" + head if len(comp) > 1 else head,
            "kind": kind, "node_ids": comp, "head": head,
            "head_host": by_id[head].get("host"), "endpoints": eps,
            "links": comp_links,
            "rail_count": sum(l["rails"] for l in comp_links),
        })
    groups.sort(key=lambda g: (len(g["node_ids"]) == 1, order[g["head"]]))
    return groups


def _engine_info(host):
    """Serving container name + uptime on a group's head node (local or SSH).

    Which names count as "the engine" comes from `engine_patterns` in the
    config, so an engine this project has never heard of still reports uptime.
    """
    pre = (_ssh_prefix(host) + " ") if host else ""
    pats = "|".join(re.escape(p) for p in CFG["engine_patterns"]) or "vllm"
    cmd = ("docker ps --format '{{.Names}}|{{.RunningFor}}' | "
           f"grep -Ei '{pats}' | head -1")
    vc = sh(f"{pre}\"{cmd}\"" if pre else cmd)
    if vc and "|" in vc:
        nm, up = vc.split("|", 1)
        return nm.strip(), up.strip()
    # Not every engine runs in a container - llama.cpp is usually a bare
    # llama-server process, so fall back to its elapsed time from ps.
    pcmd = (f"ps -eo etime=,comm= -o args= | grep -Ei '{pats}' "
            "| grep -v grep | head -1")
    pc = sh(f"{pre}\"{pcmd}\"" if pre else pcmd).strip()
    if pc:
        parts = pc.split(None, 2)
        if len(parts) >= 2:
            return parts[1].strip(), _etime(parts[0])
    return None, None


def _etime(e):
    """ps elapsed time ([[dd-]hh:]mm:ss) -> human string."""
    try:
        days, _, rest = e.partition("-")
        if not rest:
            rest, days = days, "0"
        bits = [int(x) for x in rest.split(":")]
        while len(bits) < 3:
            bits.insert(0, 0)
        h = int(days) * 24 + bits[0]
        if h >= 24:
            return f"{h // 24}d {h % 24}h"
        return f"{h}h {bits[1]}m" if h else f"{bits[1]}m"
    except Exception:
        return e


def group_metrics(g):
    """Live models + performance metrics for one topology group, with a
    per-group tok/s rate (generation_tokens_total is a counter)."""
    names, maxctx = _models_at(g["endpoints"])
    m = _metrics_for(g["endpoints"])
    gid, gt, now = g["id"], m.get("gt"), time.time()
    tok_s = None
    prev = _tokrate.get(gid)
    if gt is not None:
        if prev and gt >= prev["gt"] and now > prev["t"]:
            tok_s = round((gt - prev["gt"]) / (now - prev["t"]), 1)
        _tokrate[gid] = {"gt": gt, "t": now}
    eng_name, eng_up = _engine_info(g.get("head_host"))
    partial = bool(m.get("partial"))
    return {
        "models": names, "model_up": bool(names),
        # llama.cpp reports context on its slots; /v1/models often omits it
        "max_ctx": maxctx or m.get("n_ctx"),
        "engine_kind": m.get("engine", "vLLM"), "partial_metrics": partial,
        "slots": m.get("slots"),
        # totals below are lifetime counters on vLLM; llama.cpp has none, so
        # only its live rate is meaningful - do not present a since-boot
        # subtotal as if it were the lifetime figure.
        "tok_s": tok_s, "req_running": m.get("rr", 0), "req_waiting": m.get("rw", 0),
        "kv": m.get("kv"), "endpoints": m.get("endpoints", 0),
        "gen_tokens": None if partial else m.get("gt"),
        "req_done": m.get("rs"),
        "engine_name": eng_name, "engine_uptime": eng_up,
        "prefix_hit": (round(m["ph"] / m["pq"] * 100, 1)
                       if m.get("pq") and m.get("ph") is not None else None),
    }


def _mem_pct(pre):
    txt = sh(f"{pre} 'cat /proc/meminfo'" if pre else "cat /proc/meminfo")
    d = {}
    for L in txt.splitlines():
        p = L.split(":", 1)
        if len(p) == 2:
            try:
                d[p[0]] = int(p[1].split()[0])
            except Exception:
                pass
    if "MemTotal" in d and "MemAvailable" in d and d["MemTotal"]:
        return round((d["MemTotal"] - d["MemAvailable"]) / d["MemTotal"] * 100, 1)
    return None


def sample_node(node):
    """One compact per-node sample: gpu util/temp/power, SoC + NVMe temp, mem."""
    pre = _pfx(node)
    o = {}
    # Same fail-fast as node_stats: five probes each paying a connect timeout
    # would make the 60 s sampler overrun its own interval on an offline node.
    if pre and sh(f"{pre} 'echo up'", timeout=5) != "up":
        return o
    q = ("nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw "
         "--format=csv,noheader,nounits")
    g = sh(f"{pre} '{q}'" if pre else q, timeout=8)
    if g:
        p = [x.strip() for x in g.split(",")]
        if p[0].isdigit():
            o["g"] = int(p[0])
        if len(p) > 1 and p[1].isdigit():
            o["tg"] = int(p[1])
        try:
            o["p"] = round(float(p[2]), 1)
        except Exception:
            pass
    zc = 'cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | tr "\\n" " "'
    zs = sh(f"{pre} '{zc}'" if pre else zc, timeout=8)
    vals = [int(x) // 1000 for x in zs.split() if x.isdigit()]
    if vals:
        o["tc"] = max(vals)
    nc = "cat /sys/class/hwmon/hwmon1/temp1_input 2>/dev/null"
    nv = sh(f"{pre} '{nc}'" if pre else nc, timeout=8)
    if nv.isdigit():
        o["td"] = round(int(nv) / 1000)
    m = _mem_pct(pre)
    if m is not None:
        o["m"] = m
    return o


def sample_once():
    s, res = {"t": int(time.time())}, {}

    def one(n):
        res[n["id"]] = sample_node(n)

    th = [threading.Thread(target=one, args=(n,)) for n in NODES]
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=12)
    s["n"] = {k: v for k, v in res.items() if v}
    s.update(_engine_metrics())
    return s


def _norm(r):
    """Normalize a row to the per-node schema (older rows used flat g1/g2/m1)."""
    if "n" not in r:
        n = {}
        if "g1" in r or "m1" in r:
            n["spark1"] = {k: r[j] for k, j in (("g", "g1"), ("m", "m1")) if j in r}
        if "g2" in r:
            n["spark2"] = {"g": r["g2"]}
        r["n"] = n
    return r


def _load_rows(since):
    rows = []
    try:
        for L in open(HIST_FILE):
            try:
                r = json.loads(L)
                if r.get("t", 0) >= since:
                    rows.append(_norm(r))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    rows.sort(key=lambda r: r["t"])
    return rows


def _prune():
    try:
        rows = _load_rows(time.time() - KEEP_DAYS * 86400)
        with open(HIST_FILE, "w") as f:
            for r in rows:
                r.pop("_d", None)
                r.pop("_dr", None)
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    except Exception:
        pass


def sampler():
    n = 0
    while True:
        try:
            with open(HIST_FILE, "a") as f:
                f.write(json.dumps(sample_once(), separators=(",", ":")) + "\n")
        except Exception:
            pass
        n += 1
        if n % 1440 == 0:
            _prune()
        time.sleep(SAMPLE_EVERY)


def history(hours):
    c = _hcache.get(hours)
    if c and time.time() - c[0] < 55:
        return c[1]
    now, cf = time.time(), conf()
    rows = _load_rows(now - 28 * 86400)
    # Track prompt (prefill) and generated tokens separately — with 400K
    # contexts prompt tokens dwarf generation and would otherwise hide it.
    for cnt, dst in ((lambda r: r.get("pt", 0) + r.get("gt", 0), "_d"),
                     (lambda r: r.get("gt", 0), "_dg"),
                     (lambda r: r.get("pt", 0), "_dp"),
                     (lambda r: r.get("rs", 0), "_dr")):
        prev = None
        for r in rows:
            if any(k in r for k in ("pt", "gt", "rs")):
                cur = cnt(r)
                d = 0 if prev is None else cur - prev
                r[dst] = cur if d < 0 else max(0, d)
                prev = cur
            else:
                r[dst] = 0
    ids = [n["id"] for n in NODES]
    span = cf["load_w"] - cf["idle_w"]

    def watts(r):
        """Estimated cluster wall draw for one sample (see DEFAULT_CONF note)."""
        tot = 0.0
        for i in ids:
            g = r.get("n", {}).get(i, {}).get("g")
            if g is not None:
                tot += cf["idle_w"] + span * min(100, max(0, g)) / 100.0
        return tot

    def kwh(rs):
        return sum(watts(r) for r in rs) * (SAMPLE_EVERY / 3600.0) / 1000.0

    t0 = now - hours * 3600
    win = [r for r in rows if r["t"] >= t0]
    nb = 120 if hours <= 24 else 168
    w = hours * 3600 / nb
    B = [{"kv": [], "tok": 0.0, "wt": [],
          "gen": 0.0,
          "g": {i: [] for i in ids}, "tg": {i: [] for i in ids},
          "tc": {i: [] for i in ids}, "m": {i: [] for i in ids}} for _ in range(nb)]
    for r in win:
        b = B[min(nb - 1, int((r["t"] - t0) / w))]
        if "kv" in r:
            b["kv"].append(r["kv"])
        b["tok"] += r["_d"]
        b["gen"] += r["_dg"]
        b["wt"].append(watts(r))
        for i in ids:
            nd = r.get("n", {}).get(i, {})
            for k in ("g", "tg", "tc", "m"):
                if k in nd:
                    b[k][i].append(nd[k])

    def avg(a):
        return round(sum(a) / len(a), 1) if a else None

    heat = [[[0.0, 0] for _ in range(24)] for _ in range(7)]
    for r in rows:
        lt = time.localtime(r["t"])
        cell = heat[lt.tm_wday][lt.tm_hour]
        cell[0] += r["_d"]
        cell[1] += 1
    heatm = [[round(c[0] / c[1], 1) if c[1] else 0 for c in day] for day in heat]
    peak = max((v for day in heatm for v in day), default=0)
    busiest = ""
    if peak > 0:
        for d in range(7):
            for h in range(24):
                if heatm[d][h] == peak:
                    busiest = f"{DAYS[d]} {h:02d}:00"
    r24 = [r for r in rows if r["t"] >= now - 86400]
    r7 = [r for r in rows if r["t"] >= now - 7 * 86400]
    # thermal summary per node — the placement/cooling view
    therm = {}
    for i in ids:
        gs = [r["n"][i]["tg"] for r in r24 if "tg" in r.get("n", {}).get(i, {})]
        cs = [r["n"][i]["tc"] for r in r24 if "tc" in r.get("n", {}).get(i, {})]
        ds = [r["n"][i]["td"] for r in r24 if "td" in r.get("n", {}).get(i, {})]
        therm[i] = {"gpu_avg": avg(gs), "gpu_max": max(gs) if gs else None,
                    "soc_avg": avg(cs), "soc_max": max(cs) if cs else None,
                    "nvme_max": max(ds) if ds else None}
    def node_kwh(rs, nid):
        tot = 0.0
        for r in rs:
            g = r.get("n", {}).get(nid, {}).get("g")
            if g is not None:
                tot += cf["idle_w"] + span * min(100, max(0, g)) / 100.0
        return tot * (SAMPLE_EVERY / 3600.0) / 1000.0

    kwh24 = kwh(r24)
    # extrapolate a partial first day to a full-day rate
    hrs24 = max(0.25, (min(86400, now - rows[0]["t"]) / 3600.0) if rows else 24)
    day_kwh = kwh24 * (24.0 / hrs24) if hrs24 < 23.5 else kwh24
    out = {"t0": t0, "w": w, "ids": ids,
           "names": {n["id"]: n["name"] for n in NODES},
           "g": {i: [avg(b["g"][i]) for b in B] for i in ids},
           "tg": {i: [avg(b["tg"][i]) for b in B] for i in ids},
           "tc": {i: [avg(b["tc"][i]) for b in B] for i in ids},
           "m": {i: [avg(b["m"][i]) for b in B] for i in ids},
           "kv": [avg(b["kv"]) for b in B],
           "watts": [avg(b["wt"]) for b in B],
           "tpm": [round(b["gen"] / (w / 60), 1) for b in B],
           "ppm": [round((b["tok"] - b["gen"]) / (w / 60), 1) for b in B],
           "gen24": int(sum(r["_dg"] for r in r24)),
           "gen7": int(sum(r["_dg"] for r in r7)),
           "prompt24": int(sum(r["_dp"] for r in r24)),
           "tok24": int(sum(r["_d"] for r in r24)),
           "tok7": int(sum(r["_d"] for r in r7)),
           "req24": int(sum(r["_dr"] for r in r24)),
           "peakq": max((r.get("rr", 0) + r.get("rw", 0) for r in r24), default=0),
           "kwh24": round(kwh24, 2), "day_kwh": round(day_kwh, 2),
           "cost_day": round(day_kwh * cf["price_kwh"], 2),
           "cost_mo": round(day_kwh * 30 * cf["price_kwh"], 2),
           "cost_yr": round(day_kwh * 365 * cf["price_kwh"], 2),
           "node_kwh24": {i: round(node_kwh(r24, i), 2) for i in ids},
           "kwh7": round(kwh(r7), 2),
           "cost7": round(kwh(r7) * cf["price_kwh"], 2),
           "watts_now": round(watts(rows[-1]), 0) if rows else None,
           "therm": therm, "heat": heatm, "busiest": busiest,
           "conf": cf, "samples": len(rows),
           "sessions": usage_sessions(win),
           "partial_day": hrs24 < 23.5}
    # prefix-cache hit rate over the window (cumulative counters -> ratio)
    pq = [r["pq"] for r in win if "pq" in r]
    ph = [r["ph"] for r in win if "ph" in r]
    if pq and ph and pq[-1] > pq[0]:
        out["prefix_hit"] = round((ph[-1] - ph[0]) / (pq[-1] - pq[0]) * 100, 1)
    elif pq and ph and pq[-1] > 0:
        out["prefix_hit"] = round(ph[-1] / pq[-1] * 100, 1)
    out["events"] = cluster_events(win)
    # active minutes / duty cycle over the window
    act_rows = [r for r in win if r.get("_d", 0) > 0 or r.get("rr", 0) > 0]
    out["active_min"] = len(act_rows)
    out["duty"] = round(len(act_rows) / max(1, len(win)) * 100, 1)
    _hcache[hours] = (time.time(), out)
    return out


def usage_sessions(rows, limit=12):
    """Group contiguous active samples into usage bursts.

    Derived from our own 60 s history (token deltas + running requests), which
    is far more reliable than log-scraping: vLLM logs no per-request lines and
    the agent's own log is health-check spam.
    """
    GAP = 4  # bridge up to 4 idle samples (~4 min) inside one session
    out, cur, idle = [], None, 0
    for r in rows:
        active = r.get("_d", 0) > 0 or r.get("rr", 0) > 0
        if active:
            idle = 0
            if cur is None:
                cur = {"start": r["t"], "end": r["t"], "tok": 0, "ptok": 0, "peak": 0}
            cur["end"] = r["t"]
            cur["tok"] += r.get("_dg", 0)
            cur["ptok"] += r.get("_dp", 0)
            cur["peak"] = max(cur["peak"], r.get("rr", 0) + r.get("rw", 0))
        elif cur is not None:
            idle += 1
            if idle > GAP:
                out.append(cur)
                cur, idle = None, 0
    if cur:
        out.append(cur)
    for s in out:
        s["mins"] = max(1, round((s["end"] - s["start"]) / 60))
        s["tok"] = int(s["tok"])
        s["ptok"] = int(s["ptok"])
    return list(reversed(out))[:limit]


def cluster_events(rows, limit=10):
    """Derive events from our own history rather than by scraping logs.

    Log scraping was tried and abandoned: vLLM does not log per-request lines
    unless you turn request logging on, engine logs differ between engines and
    versions, and most of them timestamp in UTC while the rest of the UI is
    local — which made a quiet night look like an outage. Every event here
    carries an epoch ts, so the browser renders it in the viewer's own time.
    """
    ev = []
    prev = None
    for r in rows:
        # cumulative counter reset => the engine restarted
        if prev is not None and "rs" in r and "rs" in prev and r["rs"] < prev["rs"]:
            ev.append({"t": r["t"], "kind": "restart", "bad": True,
                       "txt": "vLLM engine restarted (counters reset)"})
        # a gap in sampling => dashboard or node was down
        if prev is not None and r["t"] - prev["t"] > 5 * SAMPLE_EVERY:
            mins = round((r["t"] - prev["t"]) / 60)
            ev.append({"t": r["t"], "kind": "gap", "bad": True,
                       "txt": f"monitoring gap of {mins} min (host or dashboard down)"})
        q = r.get("rr", 0) + r.get("rw", 0)
        if q >= 5 and (not ev or ev[-1].get("kind") != "load"
                       or r["t"] - ev[-1]["t"] > 1800):
            ev.append({"t": r["t"], "kind": "load", "bad": q >= 10,
                       "txt": f"high concurrency: {q} requests in flight/queued"})
        hot = [v.get("tc") for v in r.get("n", {}).values() if v.get("tc")]
        if hot and max(hot) >= 98 and (not ev or ev[-1].get("kind") != "therm"
                                       or r["t"] - ev[-1]["t"] > 3600):
            ev.append({"t": r["t"], "kind": "therm", "bad": True,
                       "txt": f"SoC hotspot reached {max(hot)}°C"})
        prev = r
    return list(reversed(ev))[:limit]


def make_icon():
    """The app icon, drawn pixel by pixel into a PNG with zlib + struct only.

    Three rising bars on the dark page colour — legible as a monitoring mark at
    both home-screen and favicon size. Generated rather than shipped so there is
    no icon file to keep in sync with the theme colours.
    """
    w = h = 180
    bg = (15, 19, 23)                               # --paper
    bars = ((157, 199, 74), (91, 163, 236), (126, 166, 205))   # --n1 --n2 --steel
    # (left, right, top) in pixels; heights rise left to right
    geom = [(38, 68, 108), (76, 106, 74), (114, 144, 46)]
    base = 142
    rows = []
    for y in range(h):
        px = bytearray()
        for x in range(w):
            col = bg
            for i, (x0, x1, top) in enumerate(geom):
                if x0 <= x < x1 and top <= y < base:
                    col = bars[i]
                    break
            px += bytes(col) + b"\xff"
        rows.append(b"\x00" + bytes(px))
    raw = b"".join(rows)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


ICON = make_icon()

# NOTE: raw string — JS escapes (\n, \d, \s) must pass through verbatim.
HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json"><link rel="apple-touch-icon" href="/icon.png">
<title>Spark Monitor</title><style>
/* Self-hosted fonts so the dashboard renders fully with no internet access
   (it is a LAN/tailnet tool). Barlow, SIL Open Font License 1.1 —
   see assets/OFL.txt. */
@font-face{font-family:Barlow;font-weight:400;font-display:swap;src:url(/assets/barlow-400.woff2) format('woff2')}
@font-face{font-family:Barlow;font-weight:500;font-display:swap;src:url(/assets/barlow-500.woff2) format('woff2')}
@font-face{font-family:Barlow;font-weight:700;font-display:swap;src:url(/assets/barlow-700.woff2) format('woff2')}
@font-face{font-family:'Barlow Condensed';font-weight:400;font-display:swap;src:url(/assets/barlowcond-400.woff2) format('woff2')}
@font-face{font-family:'Barlow Condensed';font-weight:600;font-display:swap;src:url(/assets/barlowcond-600.woff2) format('woff2')}
/* Dark is the default; [data-theme=light] flips to the steel-on-paper look.
   Legacy var names (--bg/--card/--bd/--tx/--mut/--gr/--am/--rd/--bl/--s1..4)
   are kept as aliases so every view keeps working. */
:root{
 --paper:#0f1317;--panel:#171d25;--inset:#12171e;
 --ink:#e6ebef;--dim:rgba(230,235,239,.60);--faint:rgba(230,235,239,.40);
 --line:rgba(160,190,215,.20);--soft:rgba(160,190,215,.10);
 --steel:#7ea6cd;--track:rgba(160,190,215,.14);--grid:rgba(160,190,215,.12);
 --ok:#6fae86;--warn:#d6a144;--crit:#d76a5a;
 --n1:#9dc74a;--n2:#5ba3ec;--n3:#eb9b4e;--n4:#b197e6;
 --bg:var(--paper);--card:var(--panel);--bd:var(--line);--tx:var(--ink);--mut:var(--dim);
 --gr:var(--ok);--am:var(--warn);--rd:var(--crit);--bl:var(--steel);
 --s1:var(--n1);--s2:var(--n2);--s3:var(--n3);--s4:var(--n4);
 --r:4px;--r2:5px}
[data-theme=light]{
 --paper:#f2f2f3;--panel:#e8e8eb;--inset:#e0e0e4;
 --ink:#1d1f20;--dim:rgba(29,31,32,.62);--faint:rgba(29,31,32,.42);
 --line:rgba(29,31,32,.16);--soft:rgba(29,31,32,.09);
 --steel:#5980a6;--track:rgba(29,31,32,.10);--grid:rgba(29,31,32,.10);
 --ok:#5b8c6e;--warn:#b8862f;--crit:#b64c3f;
 --n1:#7fa62f;--n2:#4a90d9;--n3:#e08b3c;--n4:#9b7fd4}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--tx);font:15px/1.55 Barlow,system-ui,-apple-system,sans-serif;padding:16px;padding-top:max(16px,env(safe-area-inset-top));max-width:1500px;margin:0 auto;transition:background .2s}
h1,h2,h3,h4,h5,h6,.cond{font-family:'Barlow Condensed',Barlow,system-ui,sans-serif;font-weight:600}
::selection{background:color-mix(in srgb,var(--steel) 30%,transparent)}
:focus-visible{outline:2px solid var(--steel);outline-offset:2px}
/* charts scale to their card but never balloon on wide desktop displays */
.card svg{width:100%;height:auto;max-height:230px;display:block}
.diagram{max-width:760px;margin:0 auto}.diagram svg{max-height:300px}
h1{font-size:26px;letter-spacing:-.01em;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h1 .dot{width:10px;height:10px;border-radius:50%;background:var(--rd)}
h1 .dot.up{background:var(--ok);box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 22%,transparent)}
.meta{color:var(--faint);font-size:12.5px;margin:5px 0 16px;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(278px,1fr));gap:12px}
/* topology | metrics, one row split in half; both cards equal height so the
   two columns line up (stacks on narrow/phone) */
.duo{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:stretch;margin-bottom:6px}
.duo>div{min-width:0;display:flex;flex-direction:column}
.duo .card{flex:1}
@media(max-width:900px){.duo{grid-template-columns:1fr}}
.card{background:var(--card);border-radius:var(--r2);padding:14px 16px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin-bottom:10px}
.bar{height:6px;background:var(--track);border-radius:2px;overflow:hidden;margin:4px 0 10px}
.bar i{display:block;height:100%;background:var(--gr);border-radius:2px;transition:width .6s}
.bar i.warn{background:var(--am)}.bar i.hot{background:var(--rd)}
.row{display:flex;justify-content:space-between;gap:12px;font-size:13px;min-width:0}.row b{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
/* Models & throughput: two columns, filling top-to-bottom then across */
.mgrid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);grid-template-rows:repeat(6,auto);grid-auto-flow:column;column-gap:28px}
.mgrid .row{padding:6px 0;border-bottom:1px solid var(--soft)}
.mgrid .row>span{white-space:nowrap}
@media(max-width:640px){.mgrid{grid-template-columns:1fr;grid-template-rows:none;grid-auto-flow:row}}
.kv{font-size:13px;color:var(--mut)}.kv b{color:var(--tx)}
.pill{display:inline-block;background:var(--inset);border:0;border-radius:var(--r);padding:2px 10px;font-size:12px;margin:2px 3px 0 0}
.foot{color:var(--mut);font-size:11px;margin-top:14px;text-align:center}
.btn{background:var(--card);border:0;color:var(--tx);border-radius:var(--r);padding:6px 14px;font-size:14px;cursor:pointer}
.alarmbtn{font-weight:700}
.alarmbtn.warn{background:color-mix(in srgb,var(--am) 18%,transparent);color:var(--am)}
.alarmbtn.crit{background:color-mix(in srgb,var(--rd) 20%,transparent);color:var(--rd);animation:flash 1s steps(1) infinite}
@keyframes flash{50%{background:var(--rd);color:var(--paper)}}
.alarmrow{display:flex;gap:8px;align-items:baseline;font-size:12.5px;padding:5px 0;border-bottom:1px solid var(--soft)}
.alarmrow:last-child{border-bottom:0}
.alarmrow .lvl{font-weight:700;font-size:10px;text-transform:uppercase;padding:1px 6px;border-radius:4px}
.lvl.crit{background:var(--rd);color:var(--paper)}.lvl.warn{background:var(--am);color:var(--paper)}
#drawer code{background:var(--inset);padding:1px 5px;border-radius:4px;font-size:12.5px}
#drawer pre{background:var(--inset);border:0;border-radius:8px;padding:10px;overflow-x:auto;font-size:12px;margin:8px 0}
#drawer table{border-collapse:collapse;width:100%;margin:10px 0;font-size:12.5px}
#drawer th,#drawer td{border:0;border-bottom:1px solid var(--soft);padding:6px 8px;text-align:left}
#drawer h2,#drawer h3{margin:14px 0 6px}#drawer h2{border-bottom:1px solid var(--soft);padding-bottom:4px}
#drawer a{color:var(--bl)}
.doc-item{padding:11px 12px;border:0;border-radius:var(--r);margin-bottom:8px;cursor:pointer;background:var(--bg)}
.chip{background:var(--card);border:0;color:var(--mut);border-radius:var(--r);padding:3px 12px;font-size:12px;cursor:pointer}
.chip.on{color:var(--steel);background:color-mix(in srgb,var(--steel) 18%,transparent)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px}
.tile{background:var(--card);border-radius:var(--r2);padding:10px 12px}
.tile b{font-size:20px;font-variant-numeric:tabular-nums}
.sechdr{display:flex;align-items:center;gap:8px;margin:20px 0 10px;flex-wrap:wrap}
.sechdr h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
.lg{display:flex;gap:12px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);margin-top:6px}
.lg i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
input[type=number],input[type=text]{background:var(--inset);border:0;color:var(--tx);border-radius:8px;padding:6px 8px;font-size:14px;width:100%}
label{font-size:12px;color:var(--mut);display:block;margin:8px 0 3px}
</style>
<script>/* set theme before first paint so there is no flash */
try{var _t=localStorage.getItem('sparkMonitorTheme');if(_t)document.documentElement.setAttribute('data-theme',_t)}catch(e){}</script>
</head><body>
<h1><span class="dot" id="dot"></span><span id="clustername">Spark Monitor</span> <span style="font-weight:400;color:var(--mut);font-size:13px" id="model"></span>
<button class="btn alarmbtn" id="alarmbtn" onclick="toggleAlarms()" style="margin-left:auto;display:none"></button>
<button class="btn" id="catbtn" onclick="toggleCat()" style="margin-left:auto">&#128230; Model Catalog</button>
<button class="btn" id="docsbtn" onclick="toggleDocs()" style="margin-left:8px">&#128218; Docs</button>
<button class="btn btn-icon" id="themebtn" onclick="toggleTheme()" title="Toggle light / dark" style="margin-left:6px">&#9789;</button></h1>
<div id="alarmpanel" style="display:none;position:absolute;top:56px;right:24px;z-index:9;background:var(--card);border:0;border-radius:12px;padding:10px 12px;max-width:460px;box-shadow:0 8px 30px rgba(0,0,0,.5)"></div>
<div class="meta" id="meta">connecting&hellip;</div>
<div class="grid" id="grid"></div>

<!-- one .duo row per topology group: visual left, model+LLM metrics right -->
<div id="rows"></div>

<div class="sechdr"><h2>Usage &amp; Trends</h2><span style="margin-left:auto"></span>
 <button class="chip on" onclick="setRange(24,this)">24h</button>
 <button class="chip" onclick="setRange(168,this)">7d</button>
 <button class="chip" onclick="setRange(720,this)">30d</button></div>
<div class="tiles" id="tiles"></div>
<div class="grid" id="trends"><div class="kv">loading history&hellip;</div></div>

<div class="sechdr"><h2>Activity</h2></div>
<div class="grid" id="activity"></div>

<div class="sechdr"><h2>Thermals &amp; Placement</h2></div>
<div class="grid" id="thermals"></div>

<div class="sechdr"><h2>Power &amp; Cost</h2>
 <button class="btn" style="margin-left:auto;font-size:12px;padding:4px 10px" onclick="toggleCost()">&#9881; Settings</button></div>
<div class="tiles" id="costtiles"></div>
<div class="card" id="costcard" style="display:none;margin-bottom:12px">
 <h2>Power cost settings</h2>
 <div class="kv" style="margin-bottom:8px">The DGX Spark exposes <b>no system power sensor</b>, so wall draw is
  <b>estimated</b> from sampled GPU utilization between the idle and load figures below.
  For real accuracy, measure one Spark with a smart plug at idle and under full load, then enter those values.</div>
 <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px">
  <div><label>Price per kWh</label><input type="number" step="0.001" id="c_price"></div>
  <div><label>Currency</label><input type="text" id="c_cur" maxlength="3"></div>
  <div><label>Idle watts / node</label><input type="number" id="c_idle"></div>
  <div><label>Load watts / node</label><input type="number" id="c_load"></div>
 </div>
 <button class="btn" style="margin-top:10px" onclick="saveCost()">Save</button>
 <span class="kv" id="csaved" style="margin-left:8px"></span>
</div>
<div class="grid" id="powertrend"></div>

<div class="foot" id="foot">Spark Monitor &middot; poll-on-access &middot; history sampled every 60s &middot; no authentication: LAN / tailnet only</div>
<div id="tip" style="position:fixed;display:none;background:var(--inset);border:0;border-radius:8px;padding:6px 10px;font-size:12px;pointer-events:none;z-index:7;max-width:220px"></div>
<div id="scrim" onclick="toggleDocs()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:8"></div>
<div id="drawer" style="position:fixed;top:0;right:0;bottom:0;width:min(560px,92vw);background:var(--card);border-left:1px solid var(--soft);z-index:9;transform:translateX(105%);transition:transform .28s ease;display:flex;flex-direction:column;padding-top:env(safe-area-inset-top)">
  <div style="display:flex;align-items:center;gap:8px;padding:14px 16px;border-bottom:1px solid var(--soft)">
    <b id="dtitle" style="font-size:15px">&#128218; Documentation</b>
    <button onclick="docHome()" id="dback" style="display:none;background:var(--inset);border:0;color:var(--mut);border-radius:var(--r);padding:3px 10px;cursor:pointer">&larr; list</button>
    <button onclick="toggleDocs()" style="margin-left:auto;background:none;border:none;color:var(--mut);font-size:20px;cursor:pointer">&#10005;</button>
  </div>
  <div id="dbody" style="overflow-y:auto;padding:14px 18px;flex:1;font-size:14px;line-height:1.6"></div>
</div>
<div id="catscrim" onclick="toggleCat()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:8"></div>
<div id="catdrawer" style="position:fixed;top:0;right:0;bottom:0;width:min(720px,94vw);background:var(--card);border-left:1px solid var(--soft);z-index:9;transform:translateX(105%);transition:transform .28s ease;display:flex;flex-direction:column;padding-top:env(safe-area-inset-top)">
  <div style="display:flex;align-items:center;gap:8px;padding:14px 16px;border-bottom:1px solid var(--soft)">
    <b style="font-size:15px">&#128230; Model Catalog</b>
    <span class="kv" id="catmeta" style="font-size:11px"></span>
    <button onclick="loadCatalog(true)" id="catref" style="margin-left:auto;background:var(--inset);border:0;color:var(--mut);border-radius:var(--r);padding:3px 10px;cursor:pointer">refresh</button>
    <button onclick="toggleCat()" style="background:none;border:none;color:var(--mut);font-size:20px;cursor:pointer">&#10005;</button>
  </div>
  <div id="catbody" style="overflow-y:auto;padding:14px 18px;flex:1;font-size:14px;line-height:1.6"></div>
</div>
<script>
const $=id=>document.getElementById(id);
const SC=['var(--s1)','var(--s2)','var(--s3)','var(--s4)'];
const bar=(p,warn=70,hot=90)=>`<div class="bar"><i class="${p>=hot?'hot':p>=warn?'warn':''}" style="width:${Math.min(100,p||0)}%"></i></div>`;
const fmt=n=>n==null?'-':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':''+Math.round(n);
let REG=[],TOPO='direct';
function nodeCard(nm,n){ if(!n||!n.online) return `<div class="card"><h2>${nm}</h2><div class="kv">unreachable</div></div>`;
 return `<div class="card"><h2>${nm}</h2>
 <div class="row"><span>GPU</span><b>${n.gpu_util??'-'}% &middot; ${n.gpu_temp??'-'}&deg;C${n.gpu_power_w?` &middot; ${n.gpu_power_w.toFixed(0)}W`:''}</b></div>${bar(n.gpu_util)}
 <div class="row"><span>CPU (load ${n.load1}/${n.cores})</span><b>${n.cpu_pct}%</b></div>${bar(n.cpu_pct)}
 <div class="row"><span>Memory</span><b>${n.mem_used_gb} / ${n.mem_total_gb} GB</b></div>${bar(n.mem_pct,80,93)}
 <div class="row"><span>Storage</span><b>${n.disk_used_gb} GB / ${n.disk_total_tb} TB</b></div>${bar(n.disk_pct,80,93)}
 <div class="kv">SoC hotspot <b>${n.soc_temp??'-'}&deg;C</b> &middot; NVMe <b>${n.nvme_temp??'-'}&deg;C</b></div>
 <div class="kv">up <b>${n.uptime||'?'}</b>${n.fabric_tx_tb!=null?` &middot; fabric tx <b>${n.fabric_tx_tb} TB</b>`:''}</div>
 <div>${(n.containers||[]).map(c=>`<span class="pill">${c.length>28?c.slice(0,28)+'&hellip;':c}</span>`).join('')}</div></div>`}
async function tick(){try{
 const s=await(await fetch('/api/stats',{cache:'no-store'})).json();
 const e=s.extras||{};REG=s.registry||[];TOPO=s.topology||'direct';
 if(s.cluster_name){$('clustername').textContent=s.cluster_name;document.title=s.cluster_name+' - Spark Monitor'}
 $('dot').className='dot'+(e.model_up?' up':'');
 /* context is shown in whichever unit reads naturally - a 32K model should not
    render as "0.0M" */
 const cx=e.max_ctx?(e.max_ctx>=1e6?(e.max_ctx/1e6).toFixed(1)+'M':Math.round(e.max_ctx/1e3)+'K'):null;
 $('model').textContent=e.model_up?(e.model+(cx?` - ctx ${cx}`:'')):'MODEL DOWN';
 const nc=e.client_count??0;
 const clientStr=nc?`${nc} client${nc>1?'s':''} connected${e.agent_local?' + local agent':''}`:(e.agent_local?'local agent only':'no clients connected');
 $('meta').textContent=`updated ${new Date(s.ts*1000).toLocaleTimeString()} - ${clientStr}`
  +(e.tailscale?` - tailscale ${e.tailscale}`:'');
 $('grid').innerHTML=REG.map(r=>nodeCard(r.name+' - '+r.role,(s.nodes||{})[r.id])).join('');
 renderRows(s);
 renderAlarms(computeAlarms(s));
}catch(err){$('meta').textContent='dashboard server unreachable - retrying...';$('dot').className='dot'}}

/* ---------- alarms: surface serious issues that need action ---------- */
function computeAlarms(s){
 const A=[],reg=s.registry||[];
 reg.forEach(r=>{
  const n=(s.nodes||{})[r.id]||{};
  if(!n.online){A.push({l:'crit',t:`${r.name} offline / unreachable`});return}
  if(n.gpu_temp>=87)A.push({l:'crit',t:`${r.name} GPU ${n.gpu_temp}&deg;C - thermal limit`});
  else if(n.gpu_temp>=84)A.push({l:'warn',t:`${r.name} GPU hot (${n.gpu_temp}&deg;C)`});
  if(n.soc_temp>=98)A.push({l:'crit',t:`${r.name} SoC hotspot ${n.soc_temp}&deg;C`});
  else if(n.soc_temp>=95)A.push({l:'warn',t:`${r.name} SoC hotspot ${n.soc_temp}&deg;C`});
  if(n.nvme_temp>=80)A.push({l:'warn',t:`${r.name} NVMe ${n.nvme_temp}&deg;C`});
  if(n.disk_pct>=95)A.push({l:'crit',t:`${r.name} storage ${n.disk_pct}% full`});
  else if(n.disk_pct>=88)A.push({l:'warn',t:`${r.name} storage ${n.disk_pct}% full`});
  const free=(n.mem_total_gb!=null&&n.mem_used_gb!=null)?n.mem_total_gb-n.mem_used_gb:null;
  if(free!=null&&free<0.5)A.push({l:'crit',t:`${r.name} unified mem ${free.toFixed(1)} GB free - near OOM`});
  else if(free!=null&&free<1.5)A.push({l:'warn',t:`${r.name} unified mem ${free.toFixed(1)} GB free`});
 });
 (s.groups||[]).forEach(g=>{
  const m=g.metrics||{},gl=groupTitle(g);
  if(!m.model_up)A.push({l:'crit',t:`${gl}: model down - no engine responding`});
  if(m.kv!=null&&m.kv>=97)A.push({l:'warn',t:`${gl}: KV cache ${m.kv}% - near full`});
  if((m.req_waiting||0)>=8)A.push({l:'warn',t:`${gl}: ${m.req_waiting} requests queued`});
 });
 return A;
}
function renderAlarms(A){
 const btn=$('alarmbtn'),panel=$('alarmpanel');
 const crit=A.filter(a=>a.l==='crit').length,warn=A.length-crit;
 if(!A.length){btn.style.display='none';panel.style.display='none';return}
 btn.style.display='';
 btn.className='btn alarmbtn '+(crit?'crit':'warn');
 btn.textContent='⚠ '+(crit?`${crit} critical`+(warn?` +${warn}`:''):`${warn} warning${warn>1?'s':''}`);
 panel.innerHTML=`<div class="kv" style="margin-bottom:6px">${A.length} issue${A.length>1?'s':''} need${A.length>1?'':'s'} attention</div>`
  +A.sort((a,b)=>(a.l==='crit'?0:1)-(b.l==='crit'?0:1))
    .map(a=>`<div class="alarmrow"><span class="lvl ${a.l}">${a.l}</span><span>${a.t}</span></div>`).join('');
}
function toggleAlarms(){const p=$('alarmpanel');p.style.display=p.style.display==='none'?'block':'none'}
/* ---------- topology: one row per group (visual left, metrics right) ---------- */
/* theme: dark by default, choice remembered */
function applyTheme(t){document.documentElement.setAttribute('data-theme',t);
 const b=$('themebtn');if(b)b.innerHTML=(t==='light')?'&#9788;':'&#9789;'}
function toggleTheme(){const t=(document.documentElement.getAttribute('data-theme')==='light')?'dark':'light';
 try{localStorage.setItem('sparkMonitorTheme',t)}catch(e){}applyTheme(t)}

function nodeById(id){return REG.find(r=>r.id===id)||{id:id,name:id}}

// SVG diagram for one topology group. Geometry follows the member count so any
// shape derived from the live cabling (solo / pair / ring / mesh) renders
// sensibly, with each node's live GPU utilization under its marker.
//
// Node marker artwork. Assets are served immutable, so the filename carries the
// version: changing the artwork means bumping this name, never just the bytes.
const NODE_IMG='/assets/ai-node-v2.webp';
function groupSVG(g,s){
 const up=id=>((s.nodes||{})[id]||{}).online;
 const ids=g.node_ids,F=ids.length,kind=g.kind;
 const solo=(kind==='solo'||F===1);
 const W=460,H=solo?172:(F===2?192:(F>=4?352:322)),cx=W/2;
 let pos={};
 if(solo){pos[ids[0]]=[cx,80]}
 else if(F===2){pos[ids[0]]=[cx-115,88];pos[ids[1]]=[cx+115,88]}
 else if(F===3){pos[ids[0]]=[cx,80];pos[ids[1]]=[cx-125,228];pos[ids[2]]=[cx+125,228]}
 else{const r=110;ids.forEach((id,i)=>{const a=-Math.PI/2+i*2*Math.PI/F;pos[id]=[cx+r*Math.cos(a),176+r*Math.sin(a)]})}
 let o=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">`;
 // links first so node markers sit on top
 const link=(a,b)=>{const [x1,y1]=pos[a],[x2,y2]=pos[b];
  return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="var(--steel)" stroke-width="2" opacity=".55" vector-effect="non-scaling-stroke"/>`};
 if(kind==='switch'&&F){const cy=176;ids.forEach(id=>{o+=`<line x1="${pos[id][0]}" y1="${pos[id][1]}" x2="${cx}" y2="${cy}" stroke="var(--steel)" stroke-width="2" opacity=".5"/>`});
  o+=`<rect x="${cx-52}" y="${cy-19}" width="104" height="38" rx="4" fill="var(--inset)" stroke="var(--bd)"/><text x="${cx}" y="${cy+5}" fill="var(--mut)" font-size="12" text-anchor="middle">fabric switch</text>`}
 else if(F===2)o+=link(ids[0],ids[1]);
 else if(F>=3)ids.forEach((id,i)=>{o+=link(id,ids[(i+1)%F])});
 const uColor=u=>u==null?'var(--mut)':u>=90?'var(--crit)':u>=70?'var(--warn)':'var(--ok)';
 ids.forEach(id=>{const r=nodeById(id),[x,y]=pos[id],st=(s.nodes||{})[id]||{},on=up(id),u=st.gpu_util;
  const rail=(r.rails&&r.rails[0])||'no fabric';
  const iw=solo?112:96,ih=iw*0.608;
  // opaque plate so the link line does not run through the label block
  o+=`<g><rect x="${x-78}" y="${y-ih/2-6}" width="160" height="${ih+80}" fill="var(--card)"/>`
   +`<image href="${NODE_IMG}" x="${x-iw/2}" y="${y-ih/2}" width="${iw}" height="${ih}" preserveAspectRatio="xMidYMid meet"${on?'':' opacity=".35"'}/>`
   +`<circle cx="${x-46}" cy="${y+ih/2+18}" r="4" fill="${on?'var(--ok)':'var(--crit)'}"/>`
   +`<text x="${x-36}" y="${y+ih/2+22}" fill="var(--tx)" font-size="14" class="cond" style="font-family:'Barlow Condensed',sans-serif;font-weight:600">${r.name}</text>`
   +`<text x="${x}" y="${y+ih/2+38}" text-anchor="middle" fill="var(--faint)" font-size="10.5">${r.role}${r.role==='head'?' &middot; rank 0':''} &middot; ${rail}</text>`
   +`<text x="${x-4}" y="${y+ih/2+60}" text-anchor="end" fill="${uColor(u)}" font-size="19" style="font-family:'Barlow Condensed',sans-serif;font-weight:600">${u!=null?u+'%':'--'}</text>`
   +(st.gpu_temp!=null?`<text x="${x+2}" y="${y+ih/2+60}" fill="var(--mut)" font-size="11">&middot; ${st.gpu_temp}&deg;C</text>`:'')
   +`</g>`});
 return `<div class="diagram">${o}</svg></div>`;
}

function groupTitle(g){
 if(g.kind==='solo')return nodeById(g.node_ids[0]).name+' - standalone';
 const rails=g.rail_count?` &middot; ${g.rail_count} rail${g.rail_count>1?'s':''}`:'';
 const label=g.kind==='switch'?'star via fabric switch'
   :g.kind==='direct'?'direct fabric link'
   :g.kind==='mesh'?'fabric mesh':'fabric ring';
 return g.node_ids.length+' nodes &middot; '+label+rails;
}
function groupHeading(g){
 return g.kind==='solo'?'Standalone'
   :g.kind==='direct'?'Direct link'
   :g.kind==='switch'?'Switched star'
   :g.kind==='mesh'?'Mesh topology':'Ring topology';
}

function groupMetrics(g,s){
 const m=g.metrics||{},models=m.models||[],eps=m.endpoints||0;
 const ctx=m.max_ctx?(m.max_ctx>=1e6?(m.max_ctx/1e6).toFixed(1)+'M':Math.round(m.max_ctx/1e3)+'K'):'-';
 const row=(k,v)=>`<div class="row"><span>${k}</span><b>${v}</b></div>`;
 // per-row GPU utilization: single aggregate (per-node detail already lives in
 // the node cards up top, so no per-node bars here).
 const gnodes=g.node_ids.map(id=>(s.nodes||{})[id]||{});
 const uv=gnodes.map(n=>n.gpu_util).filter(v=>v!=null);
 const avg=uv.length?Math.round(uv.reduce((a,b)=>a+b,0)/uv.length):null;
 const multi=g.node_ids.length>1;
 // headroom: tightest of unified-mem-free (min across nodes) and KV-cache-free
 const frees=gnodes.map(n=>(n.mem_total_gb!=null&&n.mem_used_gb!=null)?n.mem_total_gb-n.mem_used_gb:null).filter(v=>v!=null);
 const freeGB=frees.length?Math.min(...frees):null;
 const kvFree=m.kv!=null?100-m.kv:null;
 const hColor=freeGB==null?'var(--tx)':freeGB<3?'var(--rd)':freeGB<8?'var(--am)':'var(--gr)';
 const headroom=freeGB!=null
   ?`<b style="color:${hColor}">${freeGB.toFixed(1)} GB mem${kvFree!=null?` &middot; ${Math.round(kvFree)}% KV`:''}</b>`:'-';
 // one engine can advertise several served-model names (aliases); show that
 // honestly rather than counting aliases as separate live models.
 const aliased=models.length>eps&&eps>0;
 return `<div class="kv">${aliased?`Served names <span style="opacity:.7">- ${eps} engine, ${models.length} aliases</span>`:'Active models'}</div>
  <div style="margin:2px 0 10px">${models.length
    ?models.map(x=>`<span class="pill" style="color:var(--gr);background:color-mix(in srgb,var(--gr) 15%,transparent)">${x}</span>`).join('')
    :'<b style="color:var(--rd)">MODEL DOWN</b>'}</div>`
  +(aliased?`<div class="kv" style="font-size:11px;margin-top:-6px;margin-bottom:8px">both names route to the same engine</div>`:'')
  // metrics run in two columns (fills top-to-bottom, then across) so the card
  // stays short and lines up with the diagram beside it
  +`<div class="mgrid">`
  +row('Throughput', m.tok_s!=null?`${m.tok_s} tok/s`:'sampling...')
  +row('In flight / queued', `${m.req_running??0} / ${m.req_waiting??0}`)
  +row('GPU util'+(multi?' (avg)':''), avg!=null?avg+'%':'-')
  +row('Headroom', headroom)
  +row('Max context', ctx)
  +row('KV cache used', m.kv!=null?m.kv+'%':'-')
  +row('Prefix cache hit', m.prefix_hit!=null?m.prefix_hit+'%':'-')
  +row('Generated (total)', m.gen_tokens!=null?m.gen_tokens.toLocaleString():'-')
  +row('Requests completed', m.req_done!=null?m.req_done.toLocaleString():'-')
  +row('Engines live', eps+(m.slots?` &middot; ${m.slots} slot${m.slots>1?'s':''}`:''))
  +row('Engine uptime', m.engine_uptime||'-')
  +`</div>`
  // llama.cpp keeps no lifetime counters unless started with --metrics, so say
  // so instead of leaving the blanks looking like a broken panel
  +(m.partial_metrics?`<div class="kv" style="font-size:11px;margin-top:8px;opacity:.75">${m.engine_kind} &middot; live figures only. Totals and prefix-cache stats need the server started with <code>--metrics</code>.</div>`:'');
}

function renderRows(s){
 const groups=s.groups||[];
 $('rows').innerHTML=groups.map(g=>{
  const m=g.metrics||{},live=(m.models||[]).length,eps=m.endpoints||0;
  const sub=!live?'model down':(live>eps&&eps>0)?`${eps} engine - ${live} names`:`${live} model${live>1?'s':''} live`;
  /* Describe the interconnect from what the nodes actually report, so the text
     never claims a layout or a link that is no longer there. Only interfaces
     that actually carry a rail address are named — a node's other RoCE ports
     exist but are not cabled, and listing them would be misleading. */
  const ifs=[...new Set(g.node_ids.flatMap(id=>(((s.nodes||{})[id]||{}).rails||[]).map(r=>r.if)))];
  const note=g.kind==='solo'
   ?`<div class="kv" style="margin-top:8px">Not on the fabric - reachable over LAN/tailnet only. Independent engine with its own model(s).</div>`
   :`<div class="kv" style="margin-top:8px">Interconnect: ${g.rail_count||0} RoCE rail${g.rail_count===1?'':'s'}${ifs.length?` &middot; ${ifs.map(x=>`<code>${x}</code>`).join(' ')}`:''}.
     ${g.rail_count>1?'Multiple rails let NCCL stripe across them. ':''}Confirm your real throughput with a bandwidth test - see docs/MULTI-NODE.md.</div>`;
  return `<div class="duo">
   <div><div class="sechdr"><h2>${groupHeading(g)}</h2><span class="kv">${groupTitle(g)}</span></div>
    <div class="card">${groupSVG(g,s)}${note}</div></div>
   <div><div class="sechdr"><h2>Models &amp; throughput</h2><span class="kv">${sub}</span></div>
    <div class="card">${groupMetrics(g,s)}</div></div>
  </div>`;
 }).join('');
}

/* ---------- charts ---------- */
let range=24,chn=0;const CH={};
function setRange(h,btn){range=h;document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));btn.classList.add('on');loadTrends()}
function chart(sers,t0,w,unit,ymax){
 const W=600,H=150,n=(sers[0].data||[]).length;
 if(!n)return '<div class="kv">no data yet</div>';
 const dvs=sers.map(s=>s.data.map(v=>v==null?0:v));
 const max=ymax||Math.max(1,...dvs.flat());
 const X=i=>40+(W-96)*(n>1?i/(n-1):0),Y=v=>10+(H-38)*(1-Math.min(v,max)/max);
 const id='ch'+(chn++);CH[id]={sers,dvs,t0,w,n,unit};
 let out=`<svg id="${id}" viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;touch-action:pan-y">`;
 [0,.5,1].forEach(f=>{const y=10+(H-38)*f;
  out+=`<line x1="40" x2="${W-56}" y1="${y}" y2="${y}" stroke="var(--bd)"/><text x="36" y="${y+4}" fill="var(--mut)" font-size="10" text-anchor="end">${fmt(max*(1-f))}${unit}</text>`});
 sers.forEach((s,si)=>{const dv=dvs[si];
  const d=dv.map((v,i)=>`${i?'L':'M'}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join('');
  if(s.fill)out+=`<path d="${d} L${X(n-1).toFixed(1)} ${H-28} L40 ${H-28} Z" fill="${s.color}" opacity=".16"/>`;
  out+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round"/>`});
 const tf=t=>{const d=new Date(t*1000);return range<=24?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):d.toLocaleDateString([],{month:'short',day:'numeric'})};
 out+=`<text x="40" y="${H-8}" fill="var(--mut)" font-size="10">${tf(t0)}</text><text x="${W-56}" y="${H-8}" fill="var(--mut)" font-size="10" text-anchor="end">${tf(t0+n*w)}</text>`;
 out+='</svg>';
 if(sers.length>1)out+='<div class="lg">'+sers.map(s=>`<span><i style="background:${s.color}"></i>${s.name}</span>`).join('')+'</div>';
 return out}
document.addEventListener('pointermove',ev=>{
 const tip=$('tip');const svg=ev.target.closest('svg[id^=ch]');
 if(!svg||!CH[svg.id]){tip.style.display='none';return}
 const c=CH[svg.id],r=svg.getBoundingClientRect();
 const fx=(ev.clientX-r.left)/r.width*600;
 const i=Math.max(0,Math.min(c.n-1,Math.round((fx-40)/(600-96)*(c.n-1))));
 const t=new Date((c.t0+(i+0.5)*c.w)*1000);
 tip.innerHTML=`<b>${t.toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}</b>`+
  c.sers.map((s,si)=>`<div><span style="color:${s.color}">&#9632;</span> ${s.name}: <b>${fmt(c.dvs[si][i])}${c.unit}</b></div>`).join('');
 tip.style.display='block';
 tip.style.left=Math.min(window.innerWidth-210,ev.clientX+12)+'px';
 tip.style.top=(ev.clientY+14)+'px'});
function heatmap(hm){const days=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
 const max=Math.max(1,...hm.flat());
 let out='<svg viewBox="0 0 600 152" style="width:100%;height:auto">';
 for(let d=0;d<7;d++){
  out+=`<text x="30" y="${28+d*17}" fill="var(--mut)" font-size="10" text-anchor="end">${days[d]}</text>`;
  for(let h=0;h<24;h++){const v=hm[d][h];const o=v?0.15+0.85*(v/max):0.05;
   out+=`<rect x="${36+h*23}" y="${18+d*17}" width="21" height="15" rx="3" fill="var(--n1)" opacity="${o.toFixed(2)}"><title>${days[d]} ${h}:00 - ${fmt(v)} tok/min avg</title></rect>`}}
 for(let h=0;h<24;h+=6)out+=`<text x="${36+h*23}" y="146" fill="var(--mut)" font-size="10">${h}:00</text>`;
 return out+'</svg>'}
function sessionList(h){const s=h.sessions||[];
 if(!s.length)return '<div class="kv">no model usage recorded in this window</div>';
 const now=Date.now()/1000;
 return s.map(x=>{const a=new Date(x.start*1000),b=new Date(x.end*1000);
  const live=(now-x.end)<180;
  const tf=d=>d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  const day=a.toLocaleDateString([],{month:'short',day:'numeric'});
  return `<div style="display:flex;gap:8px;margin:6px 0;font-size:12.5px;align-items:baseline">
   <span style="width:8px;height:8px;border-radius:50%;background:${live?'var(--gr)':'var(--bd)'};flex:none"></span>
   <span class="kv" style="white-space:nowrap;font-size:11px">${day}</span>
   <b style="white-space:nowrap">${tf(a)}-${tf(b)}</b>
   <span class="kv" style="white-space:nowrap">${x.mins}m</span>
   <span style="margin-left:auto;white-space:nowrap"><b>${fmt(x.tok)}</b> gen <span class="kv">/ ${fmt(x.ptok)} prompt</span>${x.peak>1?` &middot; peak ${x.peak}`:''}</span>
  </div>`}).join('')+
  `<div class="kv" style="margin-top:10px;padding-top:8px;border-top:1px solid var(--soft)">Active <b>${h.active_min||0} min</b> of this window (<b>${h.duty||0}%</b> duty cycle)${h.prefix_hit!=null?` &middot; prefix cache hit <b>${h.prefix_hit}%</b>`:''}</div>`}
function ser(h,key,unit){return h.ids.map((id,i)=>({name:h.names[id]||id,color:SC[i%4],data:(h[key]||{})[id]||[]}))}
async function loadTrends(){try{
 const h=await(await fetch('/api/history?h='+range,{cache:'no-store'})).json();
 CONF=h.conf||CONF;fillConf();
 if(!h.samples){$('tiles').innerHTML='';$('trends').innerHTML='<div class="card"><div class="kv">collecting first samples - trends appear within a few minutes</div></div>';return}
 $('tiles').innerHTML=[
  ['Generated &middot; 24h',fmt(h.gen24)],['Prompt processed &middot; 24h',fmt(h.prompt24)],
  ['Requests &middot; 24h',fmt(h.req24)],['Peak queue &middot; 24h',h.peakq],
  ['Duty cycle &middot; 24h',(h.duty??0)+'%'],['Busiest hour',h.busiest||'-']
 ].map(t=>`<div class="tile"><div class="kv">${t[0]}</div><b>${t[1]}</b></div>`).join('');
 chn=0;for(const k in CH)delete CH[k];
 $('trends').innerHTML=
  `<div class="card"><h2>Throughput &middot; tokens/min</h2>${chart([{name:'generated',color:'var(--s1)',data:h.tpm,fill:1},{name:'prompt (prefill)',color:'var(--s2)',data:h.ppm}],h.t0,h.w,'')}
   <div class="kv" style="margin-top:6px;font-size:11.5px">Prompt tokens are re-sent every turn, so at large context they dwarf generation - that gap is why long sessions get slow.</div></div>`+
  `<div class="card"><h2>GPU utilization</h2>${chart(ser(h,'g'),h.t0,h.w,'%',100)}</div>`+
  `<div class="card"><h2>Capacity &middot; memory &amp; KV cache</h2>${chart(ser(h,'m').concat([{name:'KV cache',color:'var(--s3)',data:h.kv}]),h.t0,h.w,'%',100)}</div>`+
  `<div class="card"><h2>Busy times &middot; avg tok/min (28d)</h2>${heatmap(h.heat)}</div>`;
 /* activity: usage sessions + derived cluster events */
 const ev=h.events||[];
 const et=t=>new Date(t*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
 $('activity').innerHTML=
  `<div class="card"><h2>Usage sessions &middot; when it was actually used</h2>${sessionList(h)}</div>`+
  `<div class="card"><h2>Cluster events</h2>${ev.length?ev.map(e=>
    `<div style="display:flex;gap:8px;margin:5px 0;font-size:12.5px;align-items:baseline">
     <span class="pill" style="font-size:10px;color:${e.bad?'var(--rd)':'var(--mut)'}">${e.kind}</span>
     <span class="kv" style="white-space:nowrap;font-size:11px">${et(e.t)}</span>
     <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e.txt}</span></div>`).join('')
   :'<div class="kv">no restarts, gaps, overload or thermal events in this window - all healthy</div>'}
   <div class="kv" style="margin-top:10px;padding-top:8px;border-top:1px solid var(--soft);font-size:11.5px">Derived from the dashboard's own 60s samples (engine restarts, monitoring gaps, high concurrency, thermal peaks) - all times local.</div></div>`;
 /* thermals */
 const th=h.therm||{};
 let trows=h.ids.map((id,i)=>{const t=th[id]||{};
  return `<div class="row" style="margin:5px 0"><span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${SC[i%4]};margin-right:6px"></i>${h.names[id]||id}</span>
  <b>GPU ${t.gpu_avg??'-'}&deg; (max ${t.gpu_max??'-'}) &middot; SoC ${t.soc_avg??'-'}&deg; (max ${t.soc_max??'-'}) &middot; NVMe ${t.nvme_max??'-'}&deg;</b></div>`}).join('');
 const socs=h.ids.map(id=>(th[id]||{}).soc_avg).filter(v=>v!=null);
 let hint='';
 if(socs.length>1){const sp=Math.round(Math.max(...socs)-Math.min(...socs));
  const hot=h.ids.filter(id=>(th[id]||{}).soc_avg===Math.max(...socs)).map(id=>h.names[id]||id)[0];
  hint=sp>=8?`<div class="kv" style="margin-top:8px;border-left:3px solid var(--am);padding-left:8px"><b>${hot}</b> runs <b>${sp}&deg;C hotter</b> on the SoC hotspot at similar load - a placement/airflow difference worth fixing (clearance, intake, stacking, ambient).</div>`
   :`<div class="kv" style="margin-top:8px;border-left:3px solid var(--gr);padding-left:8px">SoC temps within ${sp}&deg;C across nodes - placement looks balanced.</div>`}
 $('thermals').innerHTML=
  `<div class="card"><h2>GPU temperature</h2>${chart(ser(h,'tg'),h.t0,h.w,'&deg;',100)}</div>`+
  `<div class="card"><h2>SoC hotspot temperature</h2>${chart(ser(h,'tc'),h.t0,h.w,'&deg;',110)}</div>`+
  `<div class="card"><h2>24h thermal summary</h2>${trows}${hint}
   <div class="kv" style="margin-top:8px;font-size:11.5px">SoC hotspot = hottest ACPI zone (SoC/VRM), normally far above GPU die temp. Track the <b>trend and the gap between nodes</b>, not the absolute number.</div></div>`;
 /* power + cost */
 const cur=(h.conf||{}).currency||'$';
 $('costtiles').innerHTML=[
  ['Cost / day (est)',cur+h.cost_day],['Cost / month (est)',cur+h.cost_mo],
  ['Cost / year (est)',cur+h.cost_yr],['Energy 24h',h.kwh24+' kWh'],
  ['Draw now (est)',(h.watts_now??'-')+' W']
 ].map(t=>`<div class="tile"><div class="kv">${t[0]}</div><b>${t[1]}</b></div>`).join('');
 const nk=h.node_kwh24||{},nkmax=Math.max(0.001,...Object.values(nk));
 const nkrows=h.ids.map((id,i)=>`<div class="row" style="margin-top:8px"><span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${SC[i%4]};margin-right:6px"></i>${h.names[id]||id}</span><b>${(nk[id]??0).toFixed(2)} kWh &middot; ${cur}${((nk[id]??0)*(h.conf||{}).price_kwh).toFixed(2)}</b></div>
  <div class="bar"><i style="width:${((nk[id]??0)/nkmax*100).toFixed(0)}%;background:${SC[i%4]}"></i></div>`).join('');
 $('powertrend').innerHTML=`<div class="card"><h2>Estimated cluster draw &middot; watts</h2>${chart([{name:'watts',color:'var(--s1)',data:h.watts,fill:1}],h.t0,h.w,'W')}
  <div class="kv" style="margin-top:8px">${h.partial_day?'Daily figures extrapolated from a partial first day. ':''}Estimated from GPU utilization between ${(h.conf||{}).idle_w}W idle and ${(h.conf||{}).load_w}W load per node - <b>no hardware power sensor exists on the Spark</b>. Calibrate with a smart plug via &#9881; Settings.</div></div>`+
  `<div class="card"><h2>Energy by node &middot; 24h</h2>${nkrows}
   <div class="row" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--soft)"><span>Last 7 days</span><b>${h.kwh7} kWh &middot; ${cur}${h.cost7}</b></div>
   <div class="row" style="margin-top:4px"><span>Rate</span><b>${cur}${(h.conf||{}).price_kwh} / kWh</b></div>
   <div class="kv" style="margin-top:8px;font-size:11.5px">Per-node share is derived from each node's own sampled GPU utilization, so an idle worker costs less than a busy head node.</div></div>`;
}catch(e){$('trends').innerHTML='<div class="card"><div class="kv">history unavailable - retrying...</div></div>'}}
let CONF={};
function fillConf(){if(!CONF||!CONF.currency)return;
 if(document.activeElement&&document.activeElement.tagName==='INPUT')return;
 $('c_price').value=CONF.price_kwh;$('c_cur').value=CONF.currency;
 $('c_idle').value=CONF.idle_w;$('c_load').value=CONF.load_w}
function toggleCost(){const c=$('costcard');c.style.display=c.style.display==='none'?'block':'none';fillConf()}
async function saveCost(){
 const body={price_kwh:parseFloat($('c_price').value),currency:$('c_cur').value,
  idle_w:parseFloat($('c_idle').value),load_w:parseFloat($('c_load').value)};
 try{const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  CONF=await r.json();$('csaved').textContent='saved';setTimeout(()=>$('csaved').textContent='',2000);loadTrends()}
 catch(e){$('csaved').textContent='save failed'}}
/* ---------- model catalog drawer ---------- */
let catOpen=false,catLoaded=false;
function toggleCat(){catOpen=!catOpen;
 $('catdrawer').style.transform=catOpen?'translateX(0)':'translateX(105%)';
 $('catscrim').style.display=catOpen?'block':'none';
 if(catOpen&&!catLoaded)loadCatalog();}
function gb(b){if(b===null||b===undefined)return'--';const u=['B','KB','MB','GB','TB'];
 let i=0,n=Math.abs(b);while(n>=1024&&i<4){n/=1024;i++}
 return n.toFixed(i>=3?1:0)+' '+u[i]}
function catTile(label,val,sub,colour){
 return '<div style="background:var(--inset);border-radius:var(--r);padding:10px 12px">'
  +'<div class="kv" style="font-size:11px;text-transform:uppercase;letter-spacing:.05em">'+label+'</div>'
  +'<div style="font-size:19px;margin-top:2px'+(colour?';color:'+colour:'')+'">'+val+'</div>'
  +(sub?'<div class="kv" style="font-size:11px">'+sub+'</div>':'')+'</div>'}
async function loadCatalog(force){
 const body=$('catbody');
 if(force)body.innerHTML='<div class="kv">refreshing&hellip;</div>';
 let c;try{c=await (await fetch('/api/catalog')).json()}
 catch(e){body.innerHTML='<div class="kv">catalog unreachable</div>';return}
 if(c.available===false){body.innerHTML='<div class="kv">'+(c.error||'no catalog')
  +'</div><div class="kv" style="margin-top:8px">Run <code>./spark-catalog.py --write</code>.</div>';return}
 catLoaded=true;
 const nas=c.nas||{},models=c.models||[];
 $('catmeta').textContent=(c.generated||'').replace('T',' ').replace('Z',' UTC');

 /* --- headline tiles --- */
 let h='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px">'
  +catTile('Models',models.length,models.filter(m=>(m.served_as||[]).length).length+' serving')
  +catTile('Total weights',gb(models.reduce((s,m)=>s+(m.bytes||0),0)),'unique models')
  +catTile('NAS',nas.mounted?gb(nas.used_bytes):'offline',
       nas.mounted?gb(nas.avail_bytes)+' free of '+gb(nas.total_bytes):'not mounted')
  +catTile('Reclaimable',gb(c.reclaimable_bytes||0),'duplicate copies',
       c.reclaimable_bytes?'var(--am)':null)
  +'</div>';

 /* --- one block per model: easier to scan than a wide table --- */
 for(const m of models){
  const serving=(m.served_as||[]).length;
  const nodes=(m.locations||[]).map(l=>l.node);
  /* a pinned copy is required, not waste -- never colour it as a problem */
  let badge='';
  if(m.required_reason) badge='<span style="background:var(--inset);color:var(--mut);border-radius:10px;padding:1px 9px;font-size:11px">pinned &times;'+m.copies+'</span>';
  else if(m.redundant_bytes) badge='<span style="background:var(--inset);color:var(--am);border-radius:10px;padding:1px 9px;font-size:11px">'+gb(m.redundant_bytes)+' redundant</span>';
  h+='<div style="border-top:1px solid var(--soft);padding:11px 0">'
   +'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">'
   +(serving?'<span style="color:var(--gr)">&#9679;</span>':'<span style="color:var(--soft)">&#9675;</span>')
   +'<b style="font-size:14px;word-break:break-word">'+m.name+'</b>'
   +'<span style="margin-left:auto;font-variant-numeric:tabular-nums">'+gb(m.bytes)+'</span></div>'
   +'<div class="kv" style="font-size:12px;margin-top:4px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
   +'<span>'+m.copies+(m.copies>1?' copies':' copy')+' &middot; '+nodes.join(', ')+'</span>'
   +(m.on_nas?'<span style="color:var(--gr)">&#10003; on NAS</span>'
             :'<span style="color:var(--mut)">not backed up</span>')
   +badge+'</div>'
   +(serving?'<div class="kv" style="font-size:12px;color:var(--gr)">serving '
      +[...new Set(m.served_as)].join(', ')+'</div>':'')
   +(m.required_reason?'<div class="kv" style="font-size:11px">'+m.required_reason+'</div>':'')
   +'</div>';}

 /* --- NAS breakdown --- */
 if(nas.mounted&&(nas.sections||[]).length){
  h+='<div style="border-top:1px solid var(--soft);margin-top:14px;padding-top:12px">'
   +'<div class="kv" style="text-transform:uppercase;font-size:11px;letter-spacing:.05em;margin-bottom:6px">NAS '+nas.mount+'</div>';
  for(const s of nas.sections){
   h+='<div style="display:flex;font-size:13px;padding:2px 0"><span>'+s.name+'</span>'
    +'<span class="kv" style="margin-left:auto;font-variant-numeric:tabular-nums">'
    +gb(s.bytes)+' &middot; '+s.files+' files</span></div>';}
  h+='</div>';}
 h+='<div class="kv" style="font-size:11px;margin-top:14px">Nodes serve from their local NVMe; '
  +'shared storage, when configured, holds the master copies. '
  +'Refresh with <code>./spark-catalog.py --write</code>.</div>';
 body.innerHTML=h;}

tick();setInterval(tick,5000);
loadTrends();setInterval(loadTrends,60000);
/* catalog loads lazily when the drawer first opens (see toggleCat) */
/* ---------- docs drawer ---------- */
let docsOpen=false,docList=null;
function toggleDocs(){docsOpen=!docsOpen;
 $('drawer').style.transform=docsOpen?'translateX(0)':'translateX(105%)';
 $('scrim').style.display=docsOpen?'block':'none';
 if(docsOpen&&!docList)docHome();}
async function docHome(){$('dback').style.display='none';$('dtitle').innerHTML='&#128218; Documentation';
 if(!docList){try{docList=await(await fetch('/api/docs')).json()}catch(e){$('dbody').innerHTML='<div class="kv">docs not synced</div>';return}}
 $('dbody').innerHTML=docList.map((d,i)=>`<div class="doc-item" onclick="openDocIdx(${i})"><b style="font-size:14px">${d.title}</b><div class="kv" style="font-size:11px">${d.file}</div></div>`).join('');}
function openDocIdx(i){openDoc(docList[i].file,docList[i].title)}
async function openDoc(f,t){$('dtitle').textContent=t;$('dback').style.display='inline-block';
 $('dbody').innerHTML='<div class="kv">loading&hellip;</div>';
 try{const md=await(await fetch('/docs/'+f)).text();$('dbody').innerHTML=render(md);$('dbody').scrollTop=0}
 catch(e){$('dbody').innerHTML='<div class="kv">failed to load</div>'}}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function inline(s){
 /* Code spans are lifted out BEFORE emphasis and put back after. Otherwise the
    emphasis regexes run across their contents: an asterisk inside a code span
    (a cron schedule, a glob) pairs with a real italic marker later in the line,
    corrupting both and leaving orphan asterisks on screen. Markdown treats code
    spans as opaque; so do we. \u0001 cannot occur in the escaped text. */
 const spans=[];
 return esc(s)
 .replace(/`([^`]+)`/g,function(m,c){spans.push(c);return '\u0001'+(spans.length-1)+'\u0001'})
 /* Non-greedy, and the body may contain single asterisks — otherwise a nested
    *italic* inside **bold** stopped the bold from matching at all, and both
    sets of markers printed literally. */
 .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
 /* italics AFTER bold: by this point no ** pairs remain, so a lone * is
    unambiguous. Without this rule *emphasis* rendered as literal asterisks. */
 .replace(/\*([^*\n]+?)\*/g,'<i>$1</i>')
 /* Images BEFORE links, since ![alt](src) contains [alt](src). Docs reference
    them by relative path; only the filename is kept and resolved against what
    /docs-images/ actually serves. */
 .replace(/!\[([^\]]*)\]\(([^)]+)\)/g,function(m,alt,src){
   const f=src.split('/').pop().split('?')[0];
   return '<img src="/docs-images/'+encodeURIComponent(f)+'" alt="'+alt.replace(/"/g,'&quot;')+'"'
    +' loading="lazy" style="display:block;max-width:100%;height:auto;border-radius:6px;margin:10px 0">'})
 .replace(/\[([^\]]+)\]\(([^)]+)\)/g,function(m,txt,url){
   if(url.endsWith('.md')){const f=url.split('/').pop();return '<a href="#" data-doc="'+f+'">'+txt+'</a>'}
   return '<a href="'+url+'" target="_blank">'+txt+'</a>'})
 .replace(/\u0001(\d+)\u0001/g,function(m,i){return '<code>'+spans[+i]+'</code>'})}
document.addEventListener('click',function(ev){const a=ev.target.closest('a[data-doc]');
 if(a){ev.preventDefault();openDoc(a.getAttribute('data-doc'),a.textContent)}});
function render(md){const out=[];const lines=md.split('\n');let inCode=false,code=[],tbl=[];
 const flushT=function(){if(!tbl.length)return;let h='<table>';
  tbl.forEach(function(r,ri){if(r.replace(/[|\s:-]/g,'')==='')return;const tag=ri===0?'th':'td';
   h+='<tr>'+r.split('|').slice(1,-1).map(function(c){return '<'+tag+'>'+inline(c.trim())+'</'+tag+'>'}).join('')+'</tr>'});
  out.push(h+'</table>');tbl=[]};
 /* Open list item. Markdown wraps a long item across several indented lines,
    and emitting one div per LINE broke every such item in half. The item is
    buffered and its continuation lines folded in, as markdown specifies. */
 let item=null;
 const flushL=function(){if(item===null)return;
  out.push(item.bullet
   ?'<div style="padding-left:18px;text-indent:-12px;margin:2px 0">&bull; '+inline(item.text)+'</div>'
   :'<div style="padding-left:18px;margin:2px 0">'+inline(item.text)+'</div>');
  item=null};
 /* Open paragraph. Markdown joins consecutive non-blank lines into one
    paragraph; emitting a div per line also split every **bold** or [link] that
    happened to wrap across two source lines, printing the markup literally. */
 let para=[];
 const flushP=function(){if(!para.length)return;
  out.push('<p style="margin:6px 0">'+inline(para.join(' '))+'</p>');para=[]};
 const flush=function(){flushL();flushP()};
 for(let i=0;i<lines.length;i++){const L=lines[i];
  if(L.startsWith('```')){flush();if(inCode){out.push('<pre>'+esc(code.join('\n'))+'</pre>');code=[]}inCode=!inCode;continue}
  if(inCode){code.push(L);continue}
  if(L.trim().startsWith('|')){flush();tbl.push(L);continue} flushT();
  const h=L.match(/^(#{1,4})\s+(.*)/);
  if(h){flush();const lv=h[1].length+1;out.push('<h'+lv+'>'+inline(h[2])+'</h'+lv+'>');continue}
  if(/^\s*[-*]\s+/.test(L)){flush();item={bullet:true,text:L.replace(/^\s*[-*]\s+/,'')};continue}
  if(/^\s*\d+\.\s+/.test(L)){flush();item={bullet:false,text:L.trim()};continue}
  /* indented, non-blank, and a list item is open => continuation of that item */
  if(item!==null&&/^\s{2,}\S/.test(L)){item.text+=' '+L.trim();continue}
  flushL();
  /* Blockquote: collect the whole run of '>' lines, strip the markers and
     render the inside recursively. One div per line broke **bold** wrapping
     across lines and ignored fenced code inside a quote — the same failure the
     paragraph buffer fixes, in the other branch. */
  if(L.startsWith('>')){flushP();
   const q=[];
   while(i<lines.length&&lines[i].startsWith('>')){q.push(lines[i].replace(/^>\s?/,''));i++}
   i--;
   out.push('<div style="border-left:3px solid var(--bl);padding:4px 10px;color:var(--mut);margin:6px 0">'
    +render(q.join('\n'))+'</div>');
   continue}
  if(L.trim()==='---'){flushP();out.push('<hr style="border:none;border-top:1px solid var(--soft);margin:12px 0">');continue}
  if(L.trim()===''){flushP();continue}
  para.push(L.trim())}
 flush();flushT();return out.join('')}
</script></body></html>"""

MANIFEST = json.dumps({"name": APP_NAME, "short_name": "Spark",
                       "start_url": "/", "display": "standalone",
                       "background_color": "#0f1317", "theme_color": "#0f1317",
                       "icons": [{"src": "/icon.png", "sizes": "180x180",
                                  "type": "image/png"}]})


def catalog():
    """The optional model inventory written by spark-catalog.py.

    Absent is a normal state, not an error: the catalog is an extra, and the
    drawer explains how to produce one.
    """
    try:
        with open(CATALOG_FILE) as f:
            c = json.load(f)
        c["_source"] = CATALOG_FILE
        return c
    except FileNotFoundError:
        return {"available": False,
                "error": "no catalog yet — run ./spark-catalog.py --write"}
    except (OSError, ValueError) as e:
        return {"available": False, "error": f"catalog unreadable: {e}"}


def doc_index():
    """Markdown available to the docs drawer, as {basename: absolute path}.

    Resolving names against this dictionary is what makes /docs/ safe: a
    requested name is either a key we put there ourselves or a 404, so no path
    from the URL is ever joined onto a directory.
    """
    out = {}
    # Root-level documents, because docs/ links to them by name ("../SECURITY.md"
    # resolves to SECURITY.md here) and a dead link in the drawer is a dead link
    # in the product.
    for name in ("README.md", "SECURITY.md", "CONTRIBUTING.md",
                 "CHANGELOG.md", "CODE_OF_CONDUCT.md"):
        path = os.path.join(HERE, name)
        if os.path.isfile(path):
            out[name] = path
    if DOCS_DIR and os.path.isdir(DOCS_DIR):
        for f in sorted(os.listdir(DOCS_DIR)):
            if f.endswith(".md") and os.path.isfile(os.path.join(DOCS_DIR, f)):
                out.setdefault(f, os.path.join(DOCS_DIR, f))
    return out


DOC_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                   ".jpeg": "image/jpeg", ".webp": "image/webp",
                   ".gif": "image/gif", ".svg": "image/svg+xml"}


def doc_image_index():
    """Images the docs drawer may show, as {basename: absolute path}.

    Same dictionary-resolution rule as doc_index(): a request either names a key
    we put here or gets a 404, so no URL path is ever joined onto a directory.
    """
    out = {}
    for d in ({os.path.join(DOCS_DIR, "images")} if DOCS_DIR else set()) | \
             {os.path.join(HERE, "docs", "images")}:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if os.path.splitext(f)[1].lower() in DOC_IMAGE_TYPES:
                out.setdefault(f, os.path.join(d, f))
    return out


def doc_list():
    """Drawer index: filename plus a human title taken from the first heading."""
    docs = []
    for name, path in doc_index().items():
        title = name[:-3].replace("-", " ").replace("_", " ").title()
        try:
            with open(path, encoding="utf-8") as f:
                first = f.readline().strip()
            if first.startswith("#"):
                title = first.lstrip("# ").strip() or title
        except OSError:
            pass
        docs.append({"file": name, "title": title})
    # README first, then the rest alphabetically by title.
    docs.sort(key=lambda d: (d["file"] != "README.md", d["title"].lower()))
    return docs


class H(BaseHTTPRequestHandler):
    server_version = f"SparkMonitor/{VERSION}"
    protocol_version = "HTTP/1.1"
    # Keep-alive is worth having for a page that polls every 5 s, but an idle
    # half-open connection would otherwise hold a thread forever.
    timeout = 30

    def log_message(self, *a):
        pass                    # the dashboard polls constantly; logs are noise

    def _send(self, body, ctype, code=200, cache=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        # Read-only stats, but there is no auth and no cross-origin use case:
        # keep other pages from reading this one.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), "application/json", code)

    def do_POST(self):
        # /api/settings is the only write endpoint in the whole server. It takes
        # exactly four numeric/string power-cost fields, all clamped in
        # save_conf(), and writes only its own settings file.
        if self.path != "/api/settings":
            self.send_error(404)
            return
        try:
            n = min(4096, int(self.headers.get("Content-Length", 0)))
            body = json.loads(self.rfile.read(n) or "{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except (ValueError, OSError):
            self._json(conf(), 400)
            return
        self._json(save_conf(body))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/stats":
            self._json(get_stats())
        elif path == "/api/history":
            m = re.search(r"[?&]h=(\d+)", self.path)
            h = min(720, max(1, int(m.group(1)) if m else 24))
            self._json(history(h))
        elif path == "/api/settings":
            self._json(conf())
        elif path == "/api/catalog":
            self._json(catalog())
        elif path == "/api/docs":
            self._json(doc_list())
        elif path == "/healthz":
            self._send("ok\n", "text/plain; charset=utf-8")
        elif path.startswith("/docs/"):
            fp = doc_index().get(path[len("/docs/"):])
            if not fp:
                self.send_error(404)
                return
            with open(fp, encoding="utf-8") as f:
                self._send(f.read(), "text/plain; charset=utf-8")
        elif path.startswith("/docs-images/"):
            name = unquote(path[len("/docs-images/"):])
            fp = doc_image_index().get(name)
            if not fp:
                self.send_error(404)
                return
            with open(fp, "rb") as f:
                self._send(f.read(), DOC_IMAGE_TYPES[os.path.splitext(fp)[1].lower()],
                           cache="public, max-age=86400")
        elif path.startswith("/assets/"):
            # Self-hosted fonts. Immutable, so cache hard — the dashboard may
            # be running with no internet access at all.
            name = path[len("/assets/"):]
            ctype = ASSETS.get(name)
            fp = os.path.join(ASSETS_DIR, name)
            if not ctype or not os.path.isfile(fp):
                self.send_error(404)
                return
            with open(fp, "rb") as f:
                self._send(f.read(), ctype,
                           cache="public, max-age=31536000, immutable")
        elif path == "/manifest.json":
            self._json(json.loads(MANIFEST))
        elif path == "/icon.png":
            self._send(ICON, "image/png",
                       cache="public, max-age=604800")
        elif path == "/":
            self._send(HTML, "text/html; charset=utf-8")
        else:
            self.send_error(404)


# ------------------------------------------------------------------ main ----
def check():
    """Validate the config and report what each node looks like from here.

    Run this after editing the config, or when a node shows "unreachable" — it
    separates "SSH is not set up" from "the dashboard has a bug".
    """
    print(f"{APP_NAME} {VERSION}")
    print(f"config     {CFG['_path']}"
          f"{'' if CFG['_existed'] else '  (not found — using defaults)'}")
    print(f"data dir   {CFG['data_dir']}")
    print(f"docs dir   {DOCS_DIR or '(none)'}")
    print(f"listening  http://{CFG['bind']}:{PORT}")
    print(f"nodes      {len(NODES)}\n")
    ok = True
    for n in NODES:
        where = n["host"] or "local"
        st = node_stats(n)
        if st.get("online"):
            rails = ", ".join(r["ip"] for r in st.get("rails", [])) or "none"
            print(f"  [ ok ] {n['name']:<20} {where:<18} "
                  f"gpu {st.get('gpu_util')}%  mem {st.get('mem_pct')}%  "
                  f"rails: {rails}  serving: {st.get('serving') or 'none'}")
        else:
            ok = False
            why = ("no /proc/meminfo — is this a Linux host?" if not n["host"]
                   else f"unreachable — try: ssh {n['host']} nvidia-smi")
            print(f"  [FAIL] {n['name']:<20} {where:<18} {why}")
    if not ok:
        print("\nSee docs/TROUBLESHOOTING.md. A remote node is usually "
              "unreachable because passwordless SSH is not set up yet; a local "
              "one because Spark Monitor must run on the Linux node itself.")
    return 0 if ok else 1


def main(argv=None):
    global CFG, NODES, PORT, SERVE_PORTS, ROUTER_PORT, FABRIC_PREFIX
    global DOCS_DIR, SAMPLE_EVERY, KEEP_DAYS, HIST_FILE, CONF_FILE
    global CATALOG_FILE, DEFAULT_CONF

    ap = argparse.ArgumentParser(
        prog="spark-monitor",
        description=f"{APP_NAME} — dashboard for DGX Spark clusters.")
    ap.add_argument("--config", metavar="PATH",
                    help=f"config file (default: {CONFIG_PATH_DEFAULT})")
    ap.add_argument("--port", type=int, help="override the configured port")
    ap.add_argument("--bind", help="override the configured bind address")
    ap.add_argument("--write-config", action="store_true",
                    help="write a starter config for this machine, then exit")
    ap.add_argument("--check", action="store_true",
                    help="validate config, probe every node, then exit")
    ap.add_argument("--version", action="version",
                    version=f"{APP_NAME} {VERSION}")
    a = ap.parse_args(argv)

    path = a.config or os.environ.get("SPARK_MONITOR_CONFIG") or CONFIG_PATH_DEFAULT
    if a.write_config:
        if os.path.exists(path):
            sys.exit(f"{path} already exists — edit it, or pass --config PATH")
        print(f"wrote {write_starter_config(path)}")
        print("Edit it to add your other nodes, then run: spark-monitor.py")
        return 0

    CFG = load_config(path)
    if a.port:
        CFG["port"] = a.port
    if a.bind:
        CFG["bind"] = a.bind
    NODES = CFG["nodes"]
    PORT = CFG["port"]
    SERVE_PORTS = tuple(CFG["serve_ports"])
    ROUTER_PORT = CFG["router_port"]
    FABRIC_PREFIX = CFG["fabric_prefix"]
    DOCS_DIR = CFG["docs_dir"]
    SAMPLE_EVERY = max(10, int(CFG["sample_seconds"]))
    KEEP_DAYS = max(1, int(CFG["history_days"]))
    HIST_FILE = os.path.join(CFG["data_dir"], "history.jsonl")
    CONF_FILE = os.path.join(CFG["data_dir"], "settings.json")
    CATALOG_FILE = os.path.join(CFG["data_dir"], "catalog.json")
    DEFAULT_CONF = dict(CFG["power"])

    if a.check:
        return check()

    try:
        os.makedirs(CFG["data_dir"], exist_ok=True)
    except OSError as e:
        sys.exit(f"{APP_NAME}: cannot create data dir {CFG['data_dir']}: {e}")

    # First run with no config: leave one behind so the next edit is obvious.
    if not CFG["_existed"]:
        try:
            write_starter_config(path)
            print(f"No config found — wrote a starter one at {path}")
        except OSError:
            pass                       # read-only home: defaults still work

    print(f"{APP_NAME} {VERSION} — {len(NODES)} node(s): "
          + ", ".join(n["name"] for n in NODES))
    print(f"  http://{'localhost' if CFG['bind'] in ('127.0.0.1', 'localhost') else CFG['bind']}:{PORT}")
    if CFG["bind"] not in ("127.0.0.1", "localhost"):
        print("  NOTE: no authentication. Keep this port on your LAN or "
              "tailnet — never port-forward it. See SECURITY.md.")
    _prune()
    threading.Thread(target=sampler, daemon=True).start()
    try:
        ThreadingHTTPServer((CFG["bind"], PORT), H).serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        sys.exit(f"{APP_NAME}: cannot bind {CFG['bind']}:{PORT}: {e}")


if __name__ == "__main__":
    sys.exit(main())
