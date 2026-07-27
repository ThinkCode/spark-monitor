# Installation

If you just want it running, [QUICKSTART.md](QUICKSTART.md) is faster. This page is the
reference: what the installer does, the alternatives, and how to undo it.

## Where to run it

**On one of your Spark nodes**, not on your laptop. The dashboard reads `nvidia-smi`,
`/proc` and `/sys` on the machine it runs on, and reaches other nodes over SSH.

If you have several nodes, run it on the one that is on most of the time. That machine
is called the **head** node in the config, and it is where the history file lives.

You *can* run it on a Linux box that isn't a Spark, monitoring the Sparks purely over
SSH — set every node's `host` and none to `null`. That host node's own card just won't
have GPU numbers. It will not run on macOS or Windows.

## Method 1 — the installer (recommended)

```bash
git clone https://github.com/ThinkCode/spark-monitor.git
cd spark-monitor
./install.sh
```

It will:

1. Check for `python3` ≥ 3.8 and warn if `nvidia-smi` is missing.
2. Write a starter config, if you don't already have one.
3. Install a systemd **user** service at
   `~/.config/systemd/user/spark-monitor.service`.
4. Run `loginctl enable-linger` so the service keeps running when you log out —
   which is what makes it survive a reboot on a headless node.
5. Enable and start it, wait for `/healthz` to answer, and print your URLs.

**No root required.** The only command that may ask for a password is `enable-linger`,
and the installer tells you the exact `sudo` command if it can't do it itself.

### Managing it

```bash
systemctl --user status spark-monitor       # running? since when?
systemctl --user restart spark-monitor      # apply a config change
systemctl --user stop spark-monitor         # stop until next boot
journalctl --user -u spark-monitor -f       # follow the log
journalctl --user -u spark-monitor -n 50    # last 50 lines
```

Note the `--user` flag on all of them. Leaving it off looks for a system service that
doesn't exist, and reports the unit as not found.

### Updating

```bash
cd spark-monitor
git pull
systemctl --user restart spark-monitor
```

Your config, history and settings live outside the repository, so they are never
touched by an update. Check [CHANGELOG.md](../CHANGELOG.md) for anything notable.

### Uninstalling

```bash
./install.sh --uninstall
```

Stops and removes the service, and leaves your data alone. To remove that too:

```bash
rm -rf ~/.config/spark-monitor ~/.local/share/spark-monitor
```

## Method 2 — run it in the foreground

Fine for trying things out, or if you don't use systemd:

```bash
python3 spark-monitor.py
```

Add `--port 9000` or `--bind 127.0.0.1` to override the config from the command line.
Stops when you press `Ctrl-C` or close the terminal.

## Method 3 — a system-wide service

Use this if you want the dashboard running under a dedicated account rather than your
login, or your distribution doesn't support user services well.

```bash
sudo cp contrib/spark-monitor.service /etc/systemd/system/
sudo nano /etc/systemd/system/spark-monitor.service
```

Replace the three placeholders and add a user:

```ini
ExecStart=/usr/bin/python3 -u /opt/spark-monitor/spark-monitor.py
WorkingDirectory=/opt/spark-monitor
User=youruser
Group=youruser
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now spark-monitor
```

The account you choose needs an SSH key that reaches your other nodes, or their cards
will show as unreachable.

## Method 4 — cron, for systems without systemd

Use the supplied script, which restarts the dashboard within five minutes if it dies and
starts it after a reboot:

```bash
crontab -e
```

```cron
*/5 * * * * $HOME/spark-monitor/contrib/keepalive.sh
```

**Do not inline the check into the cron line.** The obvious one-liner —
`pgrep -f "spark-monitor[.]py" >/dev/null || python3 …` — is a silent no-op.
`pgrep -f` matches every process's full command line, including the shell cron just
started to run that line, and that shell's command line necessarily contains
`spark-monitor.py`. So pgrep always finds a "match", the `||` never fires, and the
keepalive never restarts anything. Bracketing the pattern doesn't save it either: the
bracket stops the pattern matching itself, but the unbracketed path being executed is
still right there on the same command line.

Putting the logic in a script fixes it, because the script's own command line is just its
path. [contrib/keepalive.sh](../contrib/keepalive.sh) explains this at the top, and also
uses `setsid … </dev/null` — a plain `nohup … &` from cron's non-interactive shell dies
the moment cron exits.

Verify it actually works, rather than trusting it:

```bash
pkill -f "spark-monitor[.]py" && sleep 300 && curl -fsS http://127.0.0.1:8088/healthz
```

## Firewall

Most DGX OS installs have no firewall enabled, and nothing is needed. If yours does:

```bash
sudo ufw status
sudo ufw allow from 192.168.1.0/24 to any port 8088 proto tcp   # your LAN's range
```

Allow your subnet, not `any`. If you use Tailscale, allowing only the tailnet is
tighter still:

```bash
sudo ufw allow in on tailscale0 to any port 8088 proto tcp
```

## Verifying the install

```bash
python3 spark-monitor.py --check
```

Prints the resolved config paths and probes every configured node, reporting each one as
`[ ok ]` or `[FAIL]` with a reason. This is the first thing to run when a node's card
says "unreachable" — it separates an SSH problem from a dashboard problem.

```
Spark Monitor 1.0.0
config     /home/you/.config/spark-monitor/config.json
data dir   /home/you/.local/share/spark-monitor
docs dir   /home/you/spark-monitor/docs
listening  http://0.0.0.0:8088
nodes      2

  [ ok ] spark-1a2b    local     gpu 4%  mem 22.1%  rails: 10.10.7.1  serving: [8000]
  [ ok ] spark-3c4d    10.0.0.12 gpu 0%  mem 18.9%  rails: 10.10.7.2  serving: []
```

## What gets written where

Nothing is installed outside your home directory.

| Path | What it is |
|---|---|
| `~/.config/spark-monitor/config.json` | Your configuration. Yours to edit; never overwritten. |
| `~/.local/share/spark-monitor/history.jsonl` | The 60-second samples behind every chart. ~50 bytes per sample, pruned to 45 days — a few MB at most. |
| `~/.local/share/spark-monitor/settings.json` | Power-cost settings saved from the UI. Kept separate so saving never rewrites your config. |
| `~/.local/share/spark-monitor/catalog.json` | Optional, written by `spark-catalog.py`. |
| `~/.config/systemd/user/spark-monitor.service` | The service unit, if you used the installer. |

Both directories follow the XDG spec, so `XDG_CONFIG_HOME` and `XDG_DATA_HOME` are
honoured if you set them. `data_dir` in the config overrides the second one.

## Next

- More than one node → [MULTI-NODE.md](MULTI-NODE.md)
- Access from your phone → [TAILSCALE.md](TAILSCALE.md)
- Every setting explained → [CONFIGURATION.md](CONFIGURATION.md)
