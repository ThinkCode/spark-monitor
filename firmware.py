#!/usr/bin/env python3
"""Firmware and version inventory for a DGX Spark / GX10 cluster.

WHAT IT REPORTS
    Per node: BIOS/UEFI version and date (DMI), every updatable device fwupd
    knows about (embedded controller, NIC, NVMe, UEFI), the NVIDIA driver, the
    kernel, and any pending NVIDIA/firmware packages apt can see.

WHERE "IS THERE A NEWER VERSION?" COMES FROM
    fwupd  -- authoritative for device firmware. `fwupdmgr get-updates`
              reports what LVFS or the vendor actually publishes.
    apt    -- for driver and kernel packages. Bounded by how fresh the apt
              index is, because refreshing it needs root; the age is reported
              alongside so a stale answer is visible rather than implied.
    drift  -- BIOS has no public "latest" feed. Among nodes of the same
              product, anything behind the newest BIOS observed is flagged as
              DRIFT, not as a vendor release: "one node is behind another" is
              all we actually know, and saying more would be a guess.

WHAT IT DELIBERATELY DOES NOT DO
    Install anything. fwupdmgr, apt and reboot all need root, and a failed
    UEFI or embedded-controller capsule can leave a machine that does not
    boot. This module generates the exact commands for a human to run and
    stops there. See docs/FIRMWARE.md for the reasoning.

COST
    A sweep runs fwupdmgr on every node over SSH, which takes tens of seconds.
    Results are cached and refreshed on a background thread so a page load
    never waits on it; callers always get an answer plus its age.
"""
import json
import os
import shlex
import subprocess
import threading
import time

TTL = 6 * 3600          # firmware moves slowly; a sweep is expensive
REPORT_STALE = 3600     # externally reported data older than this is flagged

_cache = {"ts": 0, "data": None}
_lock = threading.RLock()
_refreshing = False


def _sh(cmd, timeout=45):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return p.stdout.strip()
    except Exception:
        return ""


def _remote(host, cmd, timeout=45):
    """Run cmd locally (host is None) or on host over SSH.

    Quoted with shlex: the DMI probe contains single quotes of its own, and
    hand-wrapping it in single quotes silently returns empty output from every
    remote node while working fine locally.
    """
    if not host:
        return _sh(cmd, timeout=timeout)
    pre = ("ssh -o BatchMode=yes -o ConnectTimeout=6 "
           "-o StrictHostKeyChecking=accept-new %s " % host)
    return _sh(pre + shlex.quote(cmd), timeout=timeout)


_DMI = ("printf '%s|%s|%s|%s|' "
        "\"$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null)\" "
        "\"$(cat /sys/class/dmi/id/product_name 2>/dev/null)\" "
        "\"$(cat /sys/class/dmi/id/bios_version 2>/dev/null)\" "
        "\"$(cat /sys/class/dmi/id/bios_date 2>/dev/null)\"; "
        "printf '%s|%s' "
        "\"$(nvidia-smi --query-gpu=driver_version --format=csv,noheader "
        "2>/dev/null | head -1)\" "
        "\"$(uname -r)\"")

# Package name fragments worth reporting. Everything else apt has pending is
# somebody else's business.
PKG_MATCH = ("nvidia", "firmware", "linux-image", "linux-headers",
             "linux-modules", "linux-nvidia", "linux-tools", "mlnx", "dgx")
KERNEL_PREFIX = ("linux-image", "linux-nvidia", "linux-modules",
                 "linux-headers", "linux-tools")


def probe_node(name, host, ssh_target=None):
    """Collect one node. Never raises: one unreachable node must not blank the
    whole report, so failures are recorded per node.

    `host` decides local vs remote; `ssh_target` is what we actually connect
    to and carries the configured ssh_user. Connecting to `host` directly
    drops that user and silently authenticates as whoever runs the dashboard.
    """
    out = {"name": name, "error": None, "vendor": "", "product": "",
           "bios": "", "bios_date": "", "driver": "", "kernel": "",
           "fw_devices": [], "fw_updates": [], "pkg_updates": [],
           "apt_cache_age_h": None, "commands": [],
           "needs_reboot": False, "needs_maintenance": False}

    conn = (ssh_target or host) if host else None
    raw = _remote(conn, _DMI, timeout=25)
    if not raw:
        out["error"] = "unreachable"
        return out
    parts = raw.split("|")
    if len(parts) >= 6:
        (out["vendor"], out["product"], out["bios"], out["bios_date"],
         out["driver"], out["kernel"]) = [p.strip() for p in parts[:6]]

    dev = _remote(conn, "timeout 40 fwupdmgr get-devices --json 2>/dev/null",
                  timeout=60)
    try:
        for d in json.loads(dev).get("Devices", []):
            flags = [f.lower() for f in d.get("Flags", [])]
            if "updatable" in flags and d.get("Version"):
                out["fw_devices"].append({"name": d.get("Name", "?"),
                                          "version": d.get("Version")})
    except Exception:
        pass

    upd = _remote(conn, "timeout 40 fwupdmgr get-updates --json 2>/dev/null",
                  timeout=60)
    try:
        for d in json.loads(upd).get("Devices", []):
            rels = d.get("Releases") or []
            flags = [f.lower() for f in d.get("Flags", [])]
            out["fw_updates"].append({
                "name": d.get("Name", "?"),
                "current": d.get("Version", "?"),
                "latest": rels[0].get("Version") if rels else "?",
                "needs_reboot": "needs-reboot" in flags,
                "needs_ac": "require-ac" in flags})
    except Exception:
        pass

    apt = _remote(conn, "apt list --upgradable 2>/dev/null", timeout=40)
    for line in apt.splitlines():
        low = line.lower()
        if "/" not in line or not any(k in low for k in PKG_MATCH):
            continue
        try:
            pkg = line.split("/", 1)[0]
            latest = line.split()[1]
            cur = (line.split("upgradable from:")[1].strip(" ]")
                   if "upgradable from:" in line else "?")
            out["pkg_updates"].append({"pkg": pkg, "current": cur,
                                       "latest": latest})
        except Exception:
            continue

    stamp = _remote(conn,
                    "stat -c %Y /var/lib/apt/periodic/update-success-stamp "
                    "2>/dev/null || stat -c %Y /var/lib/apt/lists 2>/dev/null",
                    timeout=20)
    try:
        out["apt_cache_age_h"] = round(
            (time.time() - int(stamp.split()[0])) / 3600, 1)
    except Exception:
        pass

    kernel = any(u["pkg"].startswith(KERNEL_PREFIX) for u in out["pkg_updates"])
    capsule = any(u.get("needs_reboot") for u in out["fw_updates"])
    out["needs_reboot"] = bool(kernel or capsule)
    out["needs_maintenance"] = bool(out["fw_updates"] or kernel)
    out["commands"] = _commands(out, ssh_target or host, kernel, capsule)
    return out


def _commands(node, target, kernel, capsule):
    """The commands a human runs, from what is actually pending on this node."""
    cmds = []
    if not (node["fw_updates"] or node["pkg_updates"]):
        return cmds
    if target:
        cmds.append("ssh %s" % target)
    if node["fw_updates"]:
        cmds.append("sudo fwupdmgr refresh --force")
        cmds.append("sudo fwupdmgr update          # %d device(s)"
                    % len(node["fw_updates"]))
    if node["pkg_updates"]:
        cmds.append("sudo apt update && sudo apt full-upgrade")
    if kernel or capsule:
        why = ", ".join(x for x in ("kernel update" if kernel else "",
                                    "firmware capsule" if capsule else "") if x)
        cmds.append("sudo reboot                   # required: %s" % why)
    return cmds


def collect(nodes):
    """nodes: [{"name","host","ssh"}]. host None means the local machine."""
    data = {"ts": time.time(), "nodes": []}
    results, threads = {}, []

    def run(n):
        results[n["name"]] = probe_node(n["name"], n.get("host"), n.get("ssh"))

    for n in nodes:
        t = threading.Thread(target=run, args=(n,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=150)
    data["nodes"] = [results[n["name"]] for n in nodes if n["name"] in results]

    # BIOS drift, computed within a product family. See the module docstring
    # for why this is reported as drift rather than as an available update.
    newest = {}
    for n in data["nodes"]:
        if n.get("error") or not n.get("bios") or not n.get("product"):
            continue
        if n["bios"] > newest.get(n["product"], ""):
            newest[n["product"]] = n["bios"]
    for n in data["nodes"]:
        n["bios_newest_seen"] = newest.get(n.get("product", ""), "")
        n["bios_drift"] = bool(n.get("bios") and n["bios_newest_seen"]
                               and n["bios"] < n["bios_newest_seen"])
    return data


def count_updates(data):
    """Actionable items. Drives the UI badge, so it must only count things a
    person could actually act on -- and must not report zero when we simply do
    not know."""
    n = 0
    for nd in data.get("nodes") or []:
        if nd.get("error"):
            n += 1                       # unreachable is not "up to date"
            continue
        n += len(nd.get("fw_updates") or [])
        n += len(nd.get("pkg_updates") or [])
        if nd.get("bios_drift"):
            n += 1
    ext = data.get("reported") or {}
    for item in (ext.get("items") or []):
        if item.get("update") or item.get("error"):
            n += 1
    return n


def get(nodes, report_path=None, force=False):
    """Immediate answer; refreshes on a background thread when stale."""
    global _refreshing
    with _lock:
        stale = force or not _cache["data"] or \
            (time.time() - _cache["ts"]) > TTL
        start = stale and not _refreshing
        if start:
            _refreshing = True

    if start:
        def worker():
            global _refreshing
            try:
                fresh = collect(nodes)
                with _lock:
                    _cache["data"], _cache["ts"] = fresh, fresh["ts"]
            finally:
                with _lock:
                    _refreshing = False
        threading.Thread(target=worker, daemon=True).start()

    with _lock:
        data = dict(_cache["data"]) if _cache["data"] else \
            {"ts": 0, "nodes": [], "loading": True}
        age = int(time.time() - _cache["ts"]) if _cache["ts"] else None
        refreshing = _refreshing

    data["reported"] = load_report(report_path)
    data["age_s"] = age
    data["loading"] = refreshing
    data["updates"] = count_updates(data)
    return data


# ---------------------------------------------------------------------------
# Optional: versions reported by something else
#
# Anything the dashboard host cannot reach itself -- a managed switch it has no
# credentials for, a tool installed on your laptop rather than the cluster --
# can POST its versions instead. The payload carries its own timestamp so the
# UI can say the data is stale rather than showing old versions as current.
# ---------------------------------------------------------------------------

def save_report(path, payload):
    payload = dict(payload or {})
    payload["ts"] = time.time()
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    return {"ok": True}


def load_report(path):
    if not path:
        return {}
    try:
        with open(path) as fh:
            rep = json.load(fh)
    except Exception:
        return {}
    rep["stale"] = (time.time() - rep.get("ts", 0)) > REPORT_STALE
    return rep
