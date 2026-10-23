#!/usr/bin/env python3
"""Switch a cluster between declared topology profiles.

    ./topology.py --status              what is running, and what each profile wants
    ./topology.py dev --dry-run         show the plan, change nothing
    ./topology.py dev                   switch (prompts first)
    ./topology.py dev --yes             no prompt

DESIRED STATE, NOT TRANSITIONS
    A profile declares what each node should be running. The plan is a diff of
    current against target, so adding a profile is one entry in a file rather
    than a new pair of migration scripts. Transition scripts rot: with N
    profiles you eventually need N-squared of them, and the stale ones keep
    referring to models and cabling you removed months ago.

CLIENTS SHOULD NOT NOTICE
    If your models sit behind a router or proxy on one port, clients keep
    talking to that port throughout. A stopped model simply disappears from
    its model list. That indirection is what makes a switch invisible to
    whatever is calling you.

WHAT IT DOES NOT DO
    Move weights, edit network configuration, or touch firmware. Those are
    one-time setup, not per-switch work.

Everything is read from a profiles file; see profiles.example.json and
docs/TOPOLOGY.md. Nothing about any particular cluster is baked in here.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

DEFAULT_PROFILES = os.path.expanduser(
    os.environ.get("SPARK_PROFILES",
                   "~/.config/spark-monitor/profiles.json"))

C = {"r": "\033[31m", "g": "\033[32m", "y": "\033[33m", "b": "\033[1m",
     "0": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}

# Line-buffer stdout. Python block-buffers when it is not a tty, so a switch
# whose output is piped or redirected to a log emits nothing at all until it
# finishes -- and a 20-minute operation that prints nothing is indistinguishable
# from one that has hung.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:                       # pragma: no cover - very old pythons
    pass


def say(m):  print("%s== %s ==%s" % (C["b"], m, C["0"]))
def ok(m):   print("  %sok%s   %s" % (C["g"], C["0"], m))
def warn(m): print("  %swarn%s %s" % (C["y"], C["0"], m))
def bad(m):  print("  %sFAIL%s %s" % (C["r"], C["0"], m))
def info(m): print("       %s" % m)


def load(path=None):
    with open(path or DEFAULT_PROFILES) as fh:
        return json.load(fh)


def ssh_target(cfg, node_name):
    n = cfg.get("nodes", {}).get(node_name, {})
    host = n.get("host")
    if not host:
        return None                     # local machine
    user = n.get("ssh_user") or cfg.get("ssh_user")
    return "%s@%s" % (user, host) if user else host


def sh(cfg, node_name, cmd, timeout=120):
    """Run a command on a node. Returns (rc, output)."""
    target = ssh_target(cfg, node_name)
    argv = (["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=10", target, cmd] if target
            else ["bash", "-lc", cmd])
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception as e:
        return -1, str(e)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def running(cfg, svc_name):
    """Is this service up?

    Asks its own control surface, not the model list: an engine can be started
    but still minutes away from answering, and treating "not serving yet" as
    "not running" makes a switch start a second copy on top of the first.
    """
    svc = cfg["services"][svc_name]
    ctl = svc["control"]
    kind = ctl.get("kind")
    if kind == "systemd-user":
        rc, out = sh(cfg, ctl["host"], "systemctl --user is-active %s" % ctl["unit"])
        return out.strip() == "active"
    if kind == "systemd":
        rc, out = sh(cfg, ctl["host"], "systemctl is-active %s" % ctl["unit"])
        return out.strip() == "active"
    if kind == "docker":
        rc, out = sh(cfg, ctl["host"],
                     "docker ps --filter name=%s --format '{{.Names}}'"
                     % ctl["container"])
        return bool(out.strip())
    if kind == "command":
        rc, _ = sh(cfg, ctl["host"], ctl["status"])
        return rc == 0
    return False


def current(cfg):
    return {s for s in cfg.get("services", {}) if running(cfg, s)}


def serving_models(cfg):
    """Model ids the router currently advertises, or None if unreachable.

    The distinction matters: a dead router looks exactly like a model that is
    still loading, and conflating them makes a switch wait out its whole
    timeout before blaming the wrong thing.
    """
    url = cfg.get("router_url")
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return [m["id"] for m in json.load(r).get("data", [])]
    except Exception:
        return None


def active_profile(cfg):
    """Which profile matches what is actually serving."""
    live = serving_models(cfg)
    if live is None:
        return None
    best, best_n = None, -1
    for name, p in cfg.get("profiles", {}).items():
        want = set(p.get("models", []))
        if want and want <= set(live) and len(want) > best_n:
            best, best_n = name, len(want)
    return best


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

def _cmds(ctl, action):
    kind, out = ctl.get("kind"), []
    unit, wd = ctl.get("unit"), ctl.get("watchdog")
    pfx = "systemctl --user" if kind == "systemd-user" else "systemctl"
    if kind in ("systemd-user", "systemd"):
        if action == "stop":
            # Watchdog first. Stopping a unit whose timer is still armed means
            # the timer starts it again in the middle of the switch.
            if wd:
                out.append("%s stop %s" % (pfx, wd))
            out.append("%s stop %s" % (pfx, unit))
        else:
            out.append("%s reset-failed %s 2>/dev/null; %s start %s"
                       % (pfx, unit, pfx, unit))
            if wd:
                out.append("%s start %s" % (pfx, wd))
    elif kind == "docker":
        out.append("docker %s %s" % ("stop" if action == "stop" else "start",
                                     ctl["container"]))
    elif kind == "command":
        out.append(ctl[action])
    return out


def stop(cfg, svc_name, dry):
    ctl = cfg["services"][svc_name]["control"]
    for c in _cmds(ctl, "stop"):
        info("%s$ %s" % ("[dry-run] " if dry else "", c))
        if dry:
            continue
        rc, out = sh(cfg, ctl["host"], c, timeout=700)
        if rc != 0:
            bad("%s: `%s` exited %d%s" % (svc_name, c, rc,
                                          (" - " + out) if out else ""))
            return False
    if dry:
        return True
    # Verify rather than trust the exit code: a unit with a long stop timeout
    # can report success while its containers still hold the GPU, and starting
    # the next model on top of that means both fail.
    if running(cfg, svc_name):
        bad("%s still reports running after stop" % svc_name)
        return False
    ok("%s stopped" % svc_name)
    return True


def start(cfg, svc_name, dry):
    ctl = cfg["services"][svc_name]["control"]
    for c in _cmds(ctl, "start"):
        info("%s$ %s" % ("[dry-run] " if dry else "", c))
        if not dry:
            # Detached: bringing a large model up can take many minutes and we
            # poll for readiness rather than holding the SSH channel open.
            sh(cfg, ctl["host"],
               "setsid nohup bash -c %s </dev/null >/dev/null 2>&1 &"
               % json.dumps(c), timeout=60)
    return True


def wait_memory(cfg, stopped, dry, floor=20, budget=300, want_frac=0.5):
    """Wait for nodes we just emptied to actually release their memory.

    Stopping a container returns as soon as it is gone, not when the kernel has
    reclaimed its pages. On unified-memory machines the incoming model then
    allocates from a pool the outgoing one has not finished freeing. Advisory:
    warns and proceeds, since by this point the old models are already down.
    """
    if dry or not stopped:
        return True
    nodes = sorted({n for s in stopped for n in cfg["services"][s]["nodes"]})
    say("waiting for memory release on %s" % ", ".join(nodes))
    time.sleep(floor)
    t0, low = time.time(), []
    while time.time() - t0 < budget:
        low = []
        for n in nodes:
            rc, out = sh(cfg, n, "awk '/MemTotal|MemAvailable/{print $2}' /proc/meminfo")
            try:
                total, avail = [int(x) for x in out.split()[:2]]
            except Exception:
                continue
            if avail < total * want_frac:
                low.append("%s %.0f/%.0f GiB" % (n, avail / 2 ** 20, total / 2 ** 20))
        if not low:
            ok("memory released after %d s" % (time.time() - t0 + floor))
            return True
        info("%4ds  still low: %s" % (time.time() - t0, "; ".join(low)))
        time.sleep(15)
    warn("memory did not settle within %ds: %s - starting anyway"
         % (budget, "; ".join(low)))
    return True


def wait_serving(cfg, svcs, dry):
    if dry or not svcs or not cfg.get("router_url"):
        return True
    budget = max(cfg["services"][s].get("ready_seconds", 600)
                 for s in svcs) + 300
    want = {s: cfg["services"][s].get("model") for s in svcs}
    want = {s: m for s, m in want.items() if m}
    if not want:
        return True
    say("waiting for %s (budget %d min)" % (", ".join(want), budget // 60))
    t0, missing = time.time(), 0
    while time.time() - t0 < budget:
        live = serving_models(cfg)
        if live is None:
            missing += 1
            if missing >= 5:
                bad("router at %s unreachable for %d probes - cannot tell "
                    "whether %s came up. Fix the router, then re-check."
                    % (cfg["router_url"], missing, ", ".join(want)))
                return False
            warn("router unreachable (%d)" % missing)
            time.sleep(30)
            continue
        missing = 0
        left = {s: m for s, m in want.items() if m not in live}
        if not left:
            ok("all serving after %d s" % (time.time() - t0))
            return True
        info("%4ds  still waiting: %s" % (time.time() - t0, ", ".join(left)))
        time.sleep(30)
    bad("timed out after %ds" % budget)
    return False


def hook(cfg, name, dry, **fmt):
    """Run an optional pre/post command, e.g. pausing a scheduler.

    Best effort by design: a missing hook must not block a switch. But a hook
    that FAILS is reported, because a scheduler that did not pause is exactly
    the thing you wanted to know about.
    """
    cmd = (cfg.get("hooks") or {}).get(name)
    if not cmd:
        return True
    try:
        cmd = cmd.format(**fmt)
    except Exception:
        pass
    info("%s$ %s" % ("[dry-run] " if dry else "", cmd))
    if dry:
        return True
    rc, out = sh(cfg, None, cmd, timeout=120)
    (ok if rc == 0 else warn)("hook %s: %s" % (name, (out or "rc=%d" % rc).splitlines()[0]
                                               if (out or True) else rc))
    return rc == 0


def switch(cfg, profile, yes=False, dry=False, log=print):
    prof = cfg["profiles"][profile]
    want = {v for v in prof["state"].values() if v and v != "idle"}
    cur = current(cfg)
    to_stop, to_start = sorted(cur - want), sorted(want - cur)

    say("plan: %s" % profile)
    info(prof.get("description", ""))
    print("  running : %s" % (", ".join(sorted(cur)) or "(nothing)"))
    print("  target  : %s" % ", ".join(sorted(want)))
    print("  stop    : %s" % (", ".join(to_stop) or "(none)"))
    print("  start   : %s" % (", ".join(to_start) or "(none)"))
    for line in prof.get("impact", []):
        warn(line)
    for s in to_stop:
        nodes = cfg["services"][s].get("nodes", [])
        if len(nodes) > 1:
            warn("%s spans %s - it stops in full" % (s, ", ".join(nodes)))

    if not to_stop and not to_start:
        ok("already in profile %r" % profile)
        return 0
    if not yes and not dry:
        try:
            if input("\n  proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  aborted")
                return 1
        except EOFError:
            bad("not a tty and --yes not given - refusing to switch blind")
            return 1

    hook(cfg, "pre_switch", dry, profile=profile)

    if to_stop:
        say("stopping")
        for s in to_stop:
            if not stop(cfg, s, dry):
                bad("stop failed - NOT starting anything on top of it. The "
                    "cluster is between profiles; fix the node and re-run.")
                return 1
        wait_memory(cfg, to_stop, dry)
    if to_start:
        say("starting")
        for s in to_start:
            start(cfg, s, dry)
        if not wait_serving(cfg, to_start, dry):
            bad("switch incomplete")
            return 1

    rc = 0
    health = cfg.get("health_command")
    if health and not dry:
        say("health gate")
        rc = subprocess.run(["bash", "-lc", health]).returncode
        (ok if rc == 0 else bad)("health command exited %d" % rc)
    # Only lift the pause on a clean switch: resuming scheduled work into a
    # half-switched cluster is worse than leaving it paused.
    if rc == 0:
        hook(cfg, "post_switch", dry, profile=profile)
    else:
        warn("leaving pre_switch hook in effect (health failed) - resolve, "
             "then run the post_switch command yourself")
    return rc


def maintenance(cfg, nodes_csv, yes=False, dry=False):
    """Free nodes for physical or firmware work: stop everything that uses
    them, leave everything else alone, and stop there."""
    want = [n.strip() for n in nodes_csv.split(",") if n.strip()]
    unknown = [n for n in want if n not in cfg.get("nodes", {})]
    if unknown:
        bad("unknown node(s): %s" % ", ".join(unknown))
        return 64
    affected = sorted(s for s, svc in cfg["services"].items()
                      if set(svc.get("nodes", [])) & set(want))
    cur = current(cfg)
    to_stop = [s for s in affected if s in cur]

    say("maintenance for: %s" % ", ".join(want))
    print("  will stop  : %s" % (", ".join(to_stop) or "(none)"))
    print("  left alone : %s" % (", ".join(sorted(cur - set(to_stop))) or "(none)"))
    for s in to_stop:
        nodes = cfg["services"][s].get("nodes", [])
        if len(nodes) > 1:
            warn("%s spans %s - servicing one of them stops all of it"
                 % (s, ", ".join(nodes)))
    if not to_stop:
        ok("nothing running on those nodes - they are free")
        return 0
    if not yes and not dry:
        try:
            if input("\n  proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  aborted")
                return 1
        except EOFError:
            bad("not a tty and --yes not given")
            return 1

    hook(cfg, "pre_switch", dry, profile="maintenance")
    say("stopping")
    for s in to_stop:
        if not stop(cfg, s, dry):
            bad("stop failed - the node is NOT safe to service")
            return 1
    wait_memory(cfg, to_stop, dry)
    say("ready")
    ok("free for maintenance: %s" % ", ".join(want))
    warn("units may be `enabled`, so a node that REBOOTS starts its model "
         "again on boot. Do reboots last, then switch back to a profile so "
         "the health gate runs.")
    return 0


def status(cfg):
    cur = current(cfg)
    say("running now")
    for s in cfg.get("services", {}):
        (ok if s in cur else info)("%-16s %s" % (s, cfg["services"][s].get("model", "")))
    act = active_profile(cfg)
    say("profiles")
    for name, p in cfg.get("profiles", {}).items():
        mark = "  <-- active" if name == act else ""
        want = {v for v in p["state"].values() if v and v != "idle"}
        print("  %-10s %s%s" % (name, ", ".join(sorted(want)), mark))
        info(p.get("description", ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("profile", nargs="?")
    ap.add_argument("--profiles", help="path to profiles.json")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", "-y", action="store_true")
    ap.add_argument("--maintenance-for", metavar="NODES",
                    help="stop everything using these nodes, then stop")
    a = ap.parse_args()
    try:
        cfg = load(a.profiles)
    except Exception as e:
        bad("could not read profiles: %s" % e)
        return 66
    if a.maintenance_for:
        return maintenance(cfg, a.maintenance_for, a.yes, a.dry_run)
    if a.status or not a.profile:
        status(cfg)
        return 0
    if a.profile not in cfg.get("profiles", {}):
        bad("unknown profile %r - have: %s"
            % (a.profile, ", ".join(cfg.get("profiles", {}))))
        return 64
    return switch(cfg, a.profile, a.yes, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
