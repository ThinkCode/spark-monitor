# Pausing scheduled work during a switch

A topology switch stops models. Anything that dispatches work on a schedule —
cron, a queue worker, a chat bot, an agent framework — keeps dispatching it,
into a model that is going away or still loading.

The symptom is nasty because it is quiet: no crash, just a burst of failures or
silent fallbacks to a worse model, at the moment you are least likely to be
watching. Then it stops, and you never find out it happened.

There are two ways to fix it. Use the first if you can.

---

## 1. Hooks — when the scheduler runs on the dashboard host

If whatever you need to pause runs on the same machine as the dashboard, you do
not need an agent. Put the commands in `profiles.json`:

```json
"hooks": {
  "pre_switch":  "my-scheduler pause --reason 'switch to {profile}'",
  "post_switch": "my-scheduler resume"
}
```

- `pre_switch` runs **before anything is stopped**.
- `post_switch` runs **only after a clean switch**.

That asymmetry is deliberate. If the health check fails, the resume does not
run, and you stay paused. Resuming scheduled work into a half-switched cluster
is worse than staying paused, and being paused is at least obvious.

`{profile}` is substituted. A hook that fails is reported but does not abort the
switch — a hook that is *missing* is not an error, but one that *fails* is
something you want to know about.

---

## 2. The agent — when the scheduler runs somewhere else

Common case: the dashboard runs on a cluster node, but the thing sending work
runs on your laptop. The dashboard cannot run a command there — it has no
credentials, and probably no route.

So control is inverted. `estop-agent.py` runs **next to the scheduler**, polls
the dashboard, and does the pausing locally.

```bash
cp estop-agent.example.json ~/.config/spark-monitor/estop-agent.json
$EDITOR ~/.config/spark-monitor/estop-agent.json
./estop-agent.py --once      # check the wiring before running it for real
./estop-agent.py
```

```json
{
  "dashboard": "http://dashboard-host:8088",
  "token": null,
  "poll_seconds": 5,
  "pause_command":  "my-scheduler pause --reason '{reason}'",
  "resume_command": "my-scheduler resume",
  "paused_command": "test -f ~/.my-scheduler/PAUSED"
}
```

Set `SPARK_MONITOR_TOKEN` in the environment instead of `token` if you would
rather not have it in a file.

`paused_command` is optional but worth setting: exit 0 means "currently
paused". Without it the agent tracks its own state in memory, which is fine
until the agent restarts in the middle of a switch and forgets.

### Why it acknowledges

The agent does not just poll — it **acknowledges**, and the dashboard waits for
that acknowledgement before stopping anything.

Without the handshake, a five-second poll interval is a five-second window in
which a scheduled job fires at a model that is already going down. With it,
there is no window: nothing stops until the pause is confirmed in place.

### Fail closed

If the agent is not running, a switch **requested from the web UI** aborts
rather than proceeding unpaused:

```
ABORTED: the pause agent did not acknowledge within 90s. Nothing was stopped.
```

This is deliberate. A safety step that can be skipped silently will eventually
be skipped silently — and you will find out from the failures, not from the
tool. Making the skip loud is the whole point.

Two escape hatches, because a fail-closed system with no way forward is just
broken:

- The **CLI is unaffected**. `./topology.py big --yes` runs regardless; only
  profile hooks apply.
- The UI offers an explicit **"switch anyway, without pausing"** when the agent
  is not responding. It says what you are giving up. That is a decision, not a
  silence.

---

## Adapting it to your setup

The agent runs shell commands you supply, so anything with a pause concept
works. A few shapes:

**A sentinel file your scheduler checks**

```json
"pause_command":  "touch ~/.myapp/PAUSED",
"resume_command": "rm -f ~/.myapp/PAUSED",
"paused_command": "test -f ~/.myapp/PAUSED"
```

**systemd units**

```json
"pause_command":  "systemctl --user stop my-worker.timer",
"resume_command": "systemctl --user start my-worker.timer",
"paused_command": "! systemctl --user is-active --quiet my-worker.timer"
```

**A queue you can drain**

```json
"pause_command":  "rabbitmqctl set_vm_memory_high_watermark 0.0",
"resume_command": "rabbitmqctl set_vm_memory_high_watermark 0.4"
```

**Nothing yet** — leave `pause_command` null. The agent still connects and
acknowledges, so switches are not blocked, and you can see the wiring work
before you trust it with anything.

### Running it under launchd or systemd

Keep the script and its config **out of `~/Documents`, `~/Desktop` and
`~/Downloads` on macOS.** A launchd agent is denied access to those by TCC
without explicit consent, and the failure looks like this:

```
[Errno 1] Operation not permitted
```

`~/.config` and `~/.local` are fine. On Linux there is no equivalent
restriction, but keeping config in `~/.config` is right anyway.

---

## Verifying it works

```bash
./estop-agent.py --once
```

Prints the current state, or tells you exactly what is wrong:

| Message | Cause |
|---|---|
| `dashboard returned 401` | Wrong or missing token |
| `dashboard returned 403` | Your IP is not in `control.networks` |
| `dashboard returned 404` | Control is not enabled on the dashboard |
| `dashboard unreachable` | Wrong address, or nothing listening |

Then run a switch and watch both sides. The dashboard log should show
`pause acknowledged - proceeding`, and your pause should be in effect before
anything stops.
