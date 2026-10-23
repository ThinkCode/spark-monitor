# Topology switching

Run different models at different times on the same hardware, and switch
between them from one command or one button.

**This is the only part of Spark Monitor that changes your cluster.** It is off
by default and stays off until you deliberately turn it on. Read the risks
below before you do.

---

## The idea

You declare **profiles**: what each node should be running. The switcher works
out the difference between that and what is running now, then stops and starts
the minimum needed.

```
./topology.py --status          # what is running, and what each profile wants
./topology.py big --dry-run     # show the plan, change nothing
./topology.py big               # switch (asks first)
```

Profiles are declared state, not migration steps. With transition scripts you
eventually need one per pair of profiles, and the stale ones keep referring to
models you deleted months ago. A profile is one entry in
`profiles.json`; start from `profiles.example.json`.

If your engines sit behind a router or proxy on one port, clients keep talking
to that port throughout — a stopped model just disappears from its list. That
indirection is what makes a switch invisible to whatever is calling you.

---

## The risks, plainly

**1. It stops running models.** In-flight requests fail. Anything on a schedule
keeps firing at a model that is going away — see [ESTOP.md](ESTOP.md) for how
to pause that automatically.

**2. A tensor-parallel model stops in full.** If a model spans four nodes, it
cannot survive losing one, so servicing or reusing any single node stops all
four. There is no rolling switch for a TP ring. The switcher warns you when a
service spans several nodes, but the arithmetic is worth internalising: with
four nodes committed to one model, exactly nothing else fits.

**3. Coming back takes minutes, not seconds.** Large models take a long time to
load and warm up. Budget it. A switch you start casually at 17:55 is not
finished by 18:00.

**4. It runs commands on your nodes over SSH.** Whatever you put in
`profiles.json` runs as your SSH user. Anyone who can drive the switcher can
run those commands.

**5. Startup is not always reliable.** Distributed engines occasionally fail to
form their process group and hang instead of failing. Check
[FIELD-NOTES.md](FIELD-NOTES.md#a-hung-distributed-start-looks-nothing-like-a-slow-one)
for how to tell a hang from a slow load — the difference is obvious once you
know, and baffling before.

---

## Security: what is actually safe

This is the important part, because the dashboard has **no user accounts and no
login**, by design, and switching adds the ability to stop your models
remotely.

### Why not a password box?

A password field would be worse than useless here, for a reason that is not
obvious: **CSRF**.

Your dashboard sits on a LAN with no authentication. Any web page you visit —
in the same browser, on the same laptop — can submit a form to
`http://your-dashboard:8088/api/topology/switch`. The browser sends it happily.
You see nothing. If the endpoint accepted a session cookie or a form password,
the browser would attach those credentials **for** the attacker.

So control uses a **token in a custom header** instead:

```
X-Spark-Token: <your token>
```

A cross-origin form cannot set a custom header. Doing so forces a CORS
preflight, which this server answers for nobody. The header requirement is
therefore both the authentication and the CSRF defence — and on a home network
the second matters more.

The token is also never accepted as a query parameter, so it stays out of proxy
logs and shell history.

### The layers

| Layer | What it stops |
|---|---|
| **Off by default** | An upgrade never silently gains the ability to stop your models |
| **Token in a header** | A malicious web page; a device that can reach the port |
| **Network allowlist** | A device on the network that captured or guessed a token |
| **Constant-time compare** | Recovering the token from response timing |
| **Fail closed** | `enabled: true` with no token, or a short token, refuses to enable and says so |

Default allowlist is **loopback and the Tailscale range** — deliberately not
all of RFC1918, because "anything on my LAN" includes the smart TV and whatever
your guests brought with them.

### The safest setup, in order

1. **Leave control off** unless you actually want to switch from the browser.
   The CLI over SSH needs none of this and is strictly safer.
2. **If you enable it, keep the default networks** and reach the dashboard over
   Tailscale (or an SSH tunnel) rather than exposing it on the LAN. Both
   encrypt the whole conversation.
3. **Only widen `control.networks` to your LAN if you understand that anything
   on that subnet can then attempt a switch** — and that on plain HTTP, the
   token crosses the network in clear text where it can be captured.
4. **Never put this on the public internet.** There is no rate limiting and no
   account lockout. Nothing here is designed to survive the open web.

```json
{
  "control": {
    "enabled": true,
    "token": "generate-a-long-random-one",
    "networks": ["127.0.0.0/8", "100.64.0.0/10"]
  },
  "profiles_file": "~/.config/spark-monitor/profiles.json"
}
```

Generate a token with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Or keep it out of the config file entirely and set `SPARK_MONITOR_TOKEN` in the
environment — which is what you want if your config is in a git repo.

Tokens shorter than 16 characters are refused. The server prints its control
status on startup, so you can see what you enabled:

```
control: ENABLED for 127.0.0.0/8, 100.64.0.0/10 (token required in X-Spark-Token header)
```

---

## Maintenance mode

To free nodes for firmware or physical work:

```bash
./topology.py --maintenance-for node-2,node-3
```

Stops every service that uses any of those nodes, leaves everything else
running, and stops there. It does not start anything afterwards — coming back
is a normal switch to a profile, so it gets the health gate and the resume hook
for free.

Two things it warns you about, both learned the hard way:

- A service spanning several nodes stops **in full**.
- Units that are `enabled` start again when a node **reboots**. Do reboots
  last, then switch back to a profile so the health check runs.

---

## Writing profiles.json

Start from `profiles.example.json`. The parts worth understanding:

**`control.host` is which node *drives* the service, not where it runs.** A
model spanning four nodes is usually launched from one of them. Get this wrong
and you will SSH to the wrong machine wondering why nothing stops.

**`nodes` lists everywhere the service occupies.** That is how maintenance mode
knows what stopping one node costs you.

**`watchdog`** names a timer that is stopped *before* the unit. If you have
something that restarts your models automatically, it will happily restart one
in the middle of a switch.

**`impact`** lines are shown before anything is stopped. Write down what a
person actually loses — "no fallback model while this runs" is worth more than
a list of unit names.

**`ready_seconds`** is how long the model takes to answer, used to size the
readiness timeout. Overestimate it.

**`health_command`** runs after the switch. A non-zero exit marks the switch
unclean, which leaves the pause in effect rather than resuming work into a
cluster that is not right.

---

## What it deliberately does not do

Move weights, edit network configuration, or update firmware. Those are one-time
setup or a separate, more dangerous operation — see [FIRMWARE.md](FIRMWARE.md).
