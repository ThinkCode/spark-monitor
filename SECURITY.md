# Security

## The one thing to know

**Spark Monitor has no authentication.** Anyone who can reach its port can see
everything on the dashboard.

That is a deliberate design choice for a tool meant to run on a network you control, and
it is safe there. It is **not** safe on the public internet.

**Never port-forward this port on your router.** Use [Tailscale](docs/TAILSCALE.md)
instead — it's free, takes five minutes, and is genuinely less work than port forwarding.

## What an attacker with access could see

The dashboard is read-only, so nobody can break your cluster through it. They could still
learn:

- Your hardware: node count, GPU model, memory and storage capacity, uptime
- Which models you run, their names and context sizes
- **IP addresses of clients connected to your inference API**
- Usage patterns: when you work, how heavily, for how long
- Container and process names
- Your electricity rate and estimated costs
- Anything in the markdown files you expose in the docs drawer

The client IP list and the usage heatmap are the parts most worth protecting — together
they describe your network and your daily routine.

## What an attacker could *not* do

There is no endpoint that starts, stops or restarts anything; no endpoint that shuts down
or reboots a node; no shell, no file upload, no way to change the node list, and no
credentials stored anywhere to steal.

The entire write surface is `POST /api/settings`, which accepts four fields:

| Field | Validation |
|---|---|
| `price_kwh` | float, clamped 0–10 |
| `idle_w` | int, clamped 0–1000 |
| `load_w` | int, clamped 1–2000, forced above `idle_w` |
| `currency` | 1–3 chars, matched against `[A-Za-z$€£¥₹]` or replaced with `$` |

Unknown keys are ignored, non-object bodies are rejected with 400, and the body is read
with a 4 KB cap. It writes only `settings.json` in the data directory. This has been
tested against oversized values, wrong types, injection strings, inverted ranges and
malformed JSON.

## Deliberate omissions

**No remote power control.** Wake-on-LAN and remote shutdown are common in monitoring
tools and are deliberately absent. A remote `poweroff` on a machine with no BMC strands
it — there is no way to power it back on remotely. A feature with no recovery path
doesn't belong here.

**No stored secrets.** SSH uses your existing keys; nothing is stored, so nothing can
leak. There is no password vault, no encrypted blob, no token file.

## Hardening, from least to most locked down

**1. Default — LAN only.** `bind: 0.0.0.0`, reachable on your local network. Fine for a
home network you control.

**2. Add a firewall rule.** Limit the port to your own subnet:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8088 proto tcp
```

**3. Tailscale only.** Reachable from anywhere you are, and nowhere else:

```bash
sudo ufw allow in on tailscale0 to any port 8088 proto tcp
sudo ufw deny 8088
```

**4. Loopback plus SSH tunnel.** The most locked-down option. Set `"bind": "127.0.0.1"`,
restart, then:

```bash
ssh -L 8088:127.0.0.1:8088 you@your-spark
```

The dashboard is then not on any network, and SSH does the authentication.

## While you're here: your inference API

The same warning applies with more force to whatever serves your models. vLLM, llama.cpp
and most other engines ship with **no authentication at all**, and an exposed inference
endpoint means someone else running workloads on your hardware, on your electricity bill.

Never forward `8000`, `8080`, `8888` or your engine's port either.

## Filing a report

**Please don't open a public issue for a vulnerability.** Use GitHub's private reporting:
the repository's **Security** tab → **Report a vulnerability**.

Include what an attacker can do, how to reproduce it, and the version or commit. A first
response should come within a few days; this is a spare-time project, so please be patient
and don't publish before a fix is available.

### In scope

- Anything reachable over HTTP that escapes intended behaviour: path traversal, injection
  into a shell command, unvalidated writes outside the settings file
- Ways to make the server execute code or write to unintended paths
- Information disclosure beyond what the dashboard intends to show

### Not in scope

- **"There's no authentication."** Documented and intentional; see above.
- **"Port 8088 is exposed to the internet on my machine."** That's a configuration
  choice this documentation repeatedly warns against.
- Anything requiring an attacker to already have shell access on a node.
- Denial of service by request flooding. It's a single-threaded-per-connection stdlib
  server on a trusted network; it isn't built to withstand that and doesn't need to be.

## Supported versions

The latest release on `main` is what gets fixes. There are no long-term support branches.
