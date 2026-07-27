# Remote access — your dashboard from anywhere

You want to check your Spark from your phone at work. There are two ways to do that, and
only one of them is a good idea.

**Don't port-forward.** Spark Monitor has no authentication. Forwarding port 8088 on
your router publishes your hardware inventory, model names, connected client IPs and
usage patterns to the entire internet, where it will be found by scanners within hours.

**Use Tailscale.** It's a free WireGuard-based mesh VPN. Your devices get private
addresses that work from anywhere, nothing is exposed publicly, and there is no router
configuration at all. Setup takes about five minutes and it's genuinely easier than port
forwarding.

Other approaches that are also fine: an SSH tunnel (below), WireGuard configured by
hand, or Cloudflare Tunnel with access control in front. Tailscale is documented here
because it's the least work for the best result.

---

## Step 1 — Install Tailscale on your Spark

On the Spark:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up --ssh --hostname=spark-1 --accept-dns=false
```

It prints a login URL. Open it in a browser and sign in — Google, GitHub, Microsoft or
email all work. **You complete that login yourself; the command never sees your
credentials.**

What those flags do:

- `--hostname=spark-1` — the name you'll use to reach it. Pick something you'll
  recognise.
- `--ssh` — enables Tailscale SSH, so you can SSH in from your other devices without
  copying keys around. Optional but convenient.
- `--accept-dns=false` — leaves your local DNS alone. Without it, Tailscale takes over
  DNS on the node, which can break local name resolution you depend on.

Get your new address:

```bash
tailscale ip -4
```

It'll be something like `100.101.102.103`. Every Tailscale address starts with `100.`
and never changes.

## Step 2 — Install Tailscale on your phone and laptop

- **iPhone / iPad:** App Store → Tailscale → sign in with the same account → allow the
  VPN profile → toggle it on.
- **Android:** Play Store → Tailscale → same.
- **Mac:** `brew install --cask tailscale` or the App Store.
- **Windows / Linux:** [tailscale.com/download](https://tailscale.com/download).

Sign in with **the same account** on every device. That's the only thing that links them.

## Step 3 — Open the dashboard

With Tailscale on, from anywhere in the world:

```
http://100.101.102.103:8088
```

or, using the name you chose:

```
http://spark-1:8088
```

That's it. No ports opened, nothing public.

## Step 4 — Add it to your home screen

**iPhone:** Safari → your dashboard → Share → **Add to Home Screen**.
**Android:** Chrome → menu → **Install app** / **Add to Home screen**.

It launches full-screen with its own icon and no browser chrome — it behaves like a
native app. Because it's just the page, it's always up to date; there's nothing to
update.

---

## Things worth knowing

### Leave Tailscale switched on

The most common confusion: the dashboard works away from home but not on your own Wi-Fi,
or the reverse. That's Tailscale being toggled off.

**Just leave it on.** It's WireGuard — the battery cost is negligible, only traffic to
your own devices goes through it, and then the *same URL* works everywhere. When you're
at home Tailscale routes it directly over your LAN, so there's no speed penalty.

If you'd rather not, add a second home-screen icon pointing at the LAN address
(`http://192.168.1.42:8088`) for home use. On first visit iOS may ask for **Local
Network** permission — tap Allow, or the page will just time out.

### Give every node its own Tailscale address

If you have several nodes, install Tailscale on **each** of them, not just the head node.

It's tempting to onboard one node and reach the rest by hopping through it. Then the day
that one node is down, you lose access to every perfectly healthy node behind it — right
when you most want to look at the dashboard. Independent addresses remove that shared
failure, and it costs nothing.

### Turn off key expiry

By default a machine's key expires after a few months and it silently drops off your
tailnet — which looks exactly like a dead node until you go and log in again.

In the [admin console](https://login.tailscale.com/admin/machines), find each Spark →
**Disable key expiry**. Do this now rather than discovering it later.

### Sharing access with someone else

Don't hand out your account. Use the admin console's **node sharing** to invite a
specific person to a specific machine.

Before you do: remember the dashboard has **no password**. Anyone who can reach it sees
everything on it, and if your inference API is also on the tailnet, they can use that
too. Share only with people you'd give a shell to.

### After a reboot

`tailscaled` is a systemd service, so it comes back on its own. Confirm with:

```bash
tailscale status
```

---

## Alternative: an SSH tunnel

No VPN, nothing installed, and it works today if you already have SSH access. Good for
occasional use from a laptop; awkward from a phone.

Set `"bind": "127.0.0.1"` in your config so the dashboard isn't on the network at all,
restart it, then from your laptop:

```bash
ssh -L 8088:127.0.0.1:8088 you@your-spark
```

Leave that running and open `http://localhost:8088`. This is the most locked-down option
available: the dashboard listens only on loopback, and SSH does the authentication.

---

## Also worth doing: keep your inference API off the internet

While you're here — the same warning applies to whatever is serving your models. vLLM,
llama.cpp and friends typically ship with **no authentication at all**. An exposed
inference endpoint is an open invitation to run workloads on your hardware at your
expense.

Keep those ports on your LAN or tailnet too. Never forward `8000`, `8080` or `8888`.
