#!/usr/bin/env python3
"""Pause whatever dispatches work at your models while a topology switch runs.

THE PROBLEM
    A switch stops models. Anything that sends work on a schedule -- cron jobs,
    a queue worker, an agent framework, a chat bot -- keeps sending it, into a
    model that is going away or still loading. You get a burst of failures, or
    silent fallbacks to a worse model, at the exact moment you are least
    watching.

TWO WAYS TO SOLVE IT, AND WHEN TO USE EACH

    1. HOOKS (simplest; use this if you can).
       If the thing to pause runs on the same machine as the dashboard, you do
       not need this agent at all. Put the commands in profiles.json:

           "hooks": {
             "pre_switch":  "my-scheduler pause --reason 'switch to {profile}'",
             "post_switch": "my-scheduler resume"
           }

       pre_switch runs before anything is stopped; post_switch only after a
       clean switch, so a failed one leaves you paused rather than resuming
       into a half-switched cluster.

    2. THIS AGENT (when the scheduler lives somewhere else).
       If the scheduler runs on your laptop, or anywhere the dashboard host
       cannot reach or has no credentials for, the dashboard cannot run a
       command there. So control is inverted: this agent runs NEXT TO THE
       SCHEDULER, polls the dashboard, and does the pausing locally.

       It also acknowledges, which is the part that matters. The dashboard
       waits for the ack before stopping anything, so there is no window where
       a job fires at a model that is already going down. Without the
       handshake a five-second poll is a five-second hole.

FAIL CLOSED
    If the agent is not running, a switch requested from the web UI aborts
    rather than proceeding unpaused, and says why. That is deliberate: a
    safety step that can be skipped silently will eventually be skipped
    silently. The CLI is unaffected, and the UI offers an explicit override
    for when you mean it.

CONFIGURE
    Copy estop-agent.example.json, edit, then:
        ./estop-agent.py --config ~/.config/spark-monitor/estop-agent.json
    See docs/ESTOP.md.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULTS = {
    "dashboard": "http://127.0.0.1:8088",
    "token": None,                  # or SPARK_MONITOR_TOKEN in the environment
    "poll_seconds": 5,
    # Commands. {profile} and {reason} are substituted. Leave pause null and
    # nothing happens -- useful for a dry first run.
    "pause_command": None,
    "resume_command": None,
    # Optional: how to ask whether the pause is currently in effect. Exit 0
    # means paused. Without it the agent tracks its own state, which is fine
    # until the agent restarts mid-switch.
    "paused_command": None,
    "verbose": True,
}


def log(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg["dashboard"].rstrip("/")
        self.token = cfg.get("token") or os.environ.get("SPARK_MONITOR_TOKEN")
        self._paused = False

    # -- transport ---------------------------------------------------------
    def _req(self, path, payload=None):
        url = self.base + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        if data:
            req.add_header("Content-Type", "application/json")
        if self.token:
            # Custom header, not a query parameter: it stays out of logs, and
            # a cross-origin form cannot set it. See control.py.
            req.add_header("X-Spark-Token", self.token)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    # -- local actions -----------------------------------------------------
    def _run(self, key, **fmt):
        cmd = self.cfg.get(key)
        if not cmd:
            return 0, "(no %s configured)" % key
        try:
            cmd = cmd.format(**fmt)
        except Exception:
            pass
        try:
            p = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                               text=True, timeout=120)
            out = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
            return p.returncode, (out[0] if out else "")
        except Exception as e:
            return -1, str(e)

    def paused(self):
        if self.cfg.get("paused_command"):
            rc, _ = self._run("paused_command")
            return rc == 0
        return self._paused

    def pause(self, profile):
        rc, msg = self._run("pause_command", profile=profile or "",
                            reason="topology switch to %s" % (profile or "?"))
        if rc == 0:
            self._paused = True
            log("paused: %s" % msg)
        else:
            log("PAUSE FAILED rc=%s: %s" % (rc, msg))
        return rc == 0

    def resume(self):
        rc, msg = self._run("resume_command")
        if rc == 0:
            self._paused = False
            log("resumed: %s" % msg)
        else:
            log("RESUME FAILED rc=%s: %s" % (rc, msg))
        return rc == 0

    # -- loop --------------------------------------------------------------
    def run(self):
        log("polling %s every %ss" % (self.base, self.cfg["poll_seconds"]))
        if not self.cfg.get("pause_command"):
            log("no pause_command configured - acking only, pausing nothing")
        last = object()
        while True:
            try:
                st = self._req("/api/estop")
            except urllib.error.HTTPError as e:
                # A configuration problem, not a transient one: say it every
                # time rather than looking like a network blip.
                log("dashboard returned %s - check token and control.networks"
                    % e.code)
                time.sleep(self.cfg["poll_seconds"])
                continue
            except Exception as e:
                log("dashboard unreachable (%s)" % e.__class__.__name__)
                time.sleep(self.cfg["poll_seconds"])
                continue

            want = st.get("want")
            if want != last:
                log("want=%r running=%s rc=%s"
                    % (want, st.get("running"), st.get("rc")))
                last = want

            if want and not self.paused():
                self.pause(want)
            elif not want and self.paused() and not st.get("running"):
                # Release only when the dashboard has stopped asking AND the
                # job is done. A failed switch keeps asking, so this does not
                # fire -- matching the CLI, which leaves you paused when the
                # health gate fails.
                self.resume()

            try:
                self._req("/api/estop/ack", {"paused": self.paused()})
            except Exception:
                pass
            time.sleep(self.cfg["poll_seconds"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.path.expanduser(
        "~/.config/spark-monitor/estop-agent.json"))
    ap.add_argument("--once", action="store_true",
                    help="one poll and exit; useful for testing the wiring")
    a = ap.parse_args()

    cfg = dict(DEFAULTS)
    try:
        with open(a.config) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        log("no config at %s - using defaults" % a.config)
    except Exception as e:
        log("bad config: %s" % e)
        return 66

    agent = Agent(cfg)
    if a.once:
        try:
            st = agent._req("/api/estop")
        except Exception as e:
            log("FAILED to reach dashboard: %s" % e)
            return 1
        log("ok: %s" % json.dumps(st))
        return 0
    try:
        agent.run()
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
