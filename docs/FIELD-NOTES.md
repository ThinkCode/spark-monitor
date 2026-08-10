# Field notes

Things that cost real time on a running Spark/GX10 cluster, written down so
they cost you less. Every one of these came from an actual failure, not from
imagining what might go wrong.

Ordered roughly by how much time each one wasted.

---

## A hung distributed start looks nothing like a slow one

A tensor-parallel model was brought up across four nodes. Three loaded their
weights. The fourth registered its rank and then sat there. The ring never
formed, and the whole thing waited out a 21-minute timeout.

The distinction that mattered:

```
sectors read in 5s: 8        # ~0 MiB
worker thread state: futex_do_wait
```

**Zero disk I/O.** A node genuinely loading tens of gigabytes of weights reads
at hundreds of MB/s. One that has hung reads nothing. Check before you wait:

```bash
a=$(awk '/nvme0n1 /{print $6}' /proc/diskstats); sleep 5
b=$(awk '/nvme0n1 /{print $6}' /proc/diskstats); echo $(( (b-a)*512/1024/1024 )) MiB
```

vLLM also names workers `Worker` until the rank handshake completes and
`Worker_TP1`, `Worker_TP2` afterwards — so the node that never joined is
obvious next to its peers. The head's log named the peer that dropped, which
was the same node.

Nothing was structurally wrong: RDMA devices matched a working node, both rails
were up, the rendezvous port was reachable, memory was free. A teardown and
relaunch formed the ring in 10 minutes. **Retry before investigating.**

## `RemainAfterExit=yes` makes `systemctl start` a silent no-op

The unit that launches the model above is `Type=oneshot` with
`RemainAfterExit=yes`, because the launcher exits once the engine is running.
That means the unit reads **`active` even when the engine underneath it is
wedged**.

So the obvious recovery — `systemctl --user start model.service` — does
nothing, reports success, and the retry waits out its whole timeout again.

**Stop the unit explicitly and confirm the containers are gone before
relaunching a hung service.** Do not trust `is-active` as a health signal for
anything launched this way.

## A firmware capsule reboot is 12 minutes, and uptime will lie about it

Measured on two GX10s applying BIOS, embedded-controller and UEFI capsules:
**12 minutes of total unreachability each.**

When the node came back reporting `up 5 minutes`, the outage had been 12. The
capsule is applied by firmware *before* the kernel starts, so uptime only
counts the last part. Do not report the outage from the returning node's
uptime; measure it from when SSH stopped answering.

Budget 15 minutes per node with capsules. A kernel-only reboot is 1–2.

Do not power-cycle a slow boot. A half-written embedded-controller capsule is
how these boards get bricked.

## Retry once before concluding anything is broken

Two separate one-off failures in one evening, both fixed by a plain retry:

```
Update State:  Failed
Update Error:  failed to run update on reboot: expected 0x00000516 and got 0x00000001
```

The identical device on an identical node had succeeded on its first attempt.
The ESP had 500 MB free and Secure Boot was on, so it was neither space nor
signing. `sudo fwupdmgr update` again, reboot, applied.

The distributed-startup hang above was the same shape. Both looked like
something deep; both were transient.

Check `fwupdmgr get-history` for the real state — a device sitting silently at
its old version is not always reported as pending.

## Do the least critical node first

Two nodes needed the same capsules. One was carrying a model that four nodes
depended on; the other was not.

Flashing the **non-critical** one first cost nothing and turned the second
flash into a known quantity: same hardware, same capsules, already proven. Had
the first flash bricked a machine, the important workload would still have been
intact.

## Verify the thing that *applies*, not the thing that *stages*

Some firmware — RouterBOOT on MikroTik switches is the example that caught us —
is written by one command and only takes effect on reboot. Worse, the field
that reads like "the new version" is what the installed package *carries*, not
what the board is *running*:

```
current-firmware: 7.20.8      <- what it is actually running
upgrade-firmware: 7.23.3      <- what is available to apply
```

Rebooting without running the upgrade command is an outage that changes
nothing. Always re-read the "current" field after the reboot.

## A hardened systemd unit strips `ping`

A health check reported the fabric switch as unreachable. The switch was up and
answering pings — from an interactive shell.

The check ran as a child of a service unit with:

```
NoNewPrivileges=yes
RestrictSUIDSGID=yes
```

`ping` needs setuid-root or `cap_net_raw`, and `NoNewPrivileges` strips both. So
it failed **only** under the service, and worked every time it was tested by
hand.

Do not weaken the unit. Make reachability checks fall back to a TCP connect:

```bash
ping -c2 -W3 "$host" >/dev/null 2>&1 || (exec 3<>/dev/tcp/"$host"/22) 2>/dev/null
```

Reachability is the question; ICMP is just one way to ask it.

## Block-buffered output makes a long operation look hung

Python block-buffers stdout when it is not a TTY. A switch whose output is
redirected to a log therefore prints **nothing at all** until it exits — so a
20-minute operation is indistinguishable from one that has crashed, for 20
minutes.

Measured: first line at 6.0 s block-buffered versus 0.0 s line-buffered.

```python
sys.stdout.reconfigure(line_buffering=True)
```

Or launch with `python3 -u`. It also fixes ordering: a child process writing to
the same descriptor unbuffered otherwise appears *above* lines its parent
"printed" earlier but has not flushed.

## `pgrep -f` matches your own command line

```bash
pgrep -f topology-switch     # matches the ssh/bash you are running right now
```

This bit three times in one session — reporting dead processes as alive,
and killing the shell doing the checking. Use `pgrep -x`, match on the
interpreter, or read `/proc/<pid>/cmdline` for the specific process.

## `"active" in out` is true for `inactive`

```python
return "active" in systemctl_output      # matches "ActiveState=inactive"
```

A dead unit read as alive. Parse the value, not the line:

```python
state = out.strip().split("=", 1)[-1].strip()
return state in ("active", "activating")
```

## Unified memory needs time to release

`docker stop` returns when the container is gone, not when the kernel has
reclaimed its pages. On unified-memory machines the incoming model then
allocates from a pool the outgoing one has not finished freeing, and both lose.

Poll `MemAvailable` instead of sleeping a guessed number of seconds. In
practice release took ~20 s — but the value is that a slow release now blocks
the start instead of racing it.

## Tailscale SSH that is denied by ACL hangs rather than fails

A health check hung for nine minutes on:

```bash
ssh -o ConnectTimeout=6 <tailnet-address> hostname
```

`ConnectTimeout` bounds the **TCP connect**. Tailscale SSH accepts the
connection and then refuses authorisation, so the timeout never fires and the
command waits forever.

Wrap remote calls in a real timeout, and prefer an address that works from
every host that might run the check. A check that can block forever will
eventually block a deployment.

## macOS: launchd cannot read `~/Documents`

An agent under launchd failed with:

```
[Errno 1] Operation not permitted
```

macOS TCC denies background agents access to `Documents`, `Desktop` and
`Downloads` without explicit user consent. Keep anything a launchd job reads in
`~/.config` or `~/.local`.

## `window.confirm()` returns false without showing anything

In a sandboxed WebView — an app embedding your dashboard, for example — a
`confirm()` call is ignored and returns `false`. The button appears completely
dead: no dialog, no error, no request.

Do not gate an operation behind a native modal. Render the confirmation into
the page.

## A disabled control must look disabled and say why

Related, and more embarrassing: a button rendered with `disabled` but no
`:disabled` styling looks identical to a live one and silently swallows every
click. The gate was working exactly as designed; it just had no way to say so.

If you refuse an action, say which check failed and what would fix it.

## Non-reentrant locks in a threaded server

```python
with _lock:
    log(helper_that_also_takes_the_lock())    # deadlock
```

`threading.Lock` is not reentrant. This wedged the lock on an error path, and
every request touching that state hung — while the process stayed alive and
systemd still reported the service healthy.

Use `threading.RLock` for state that helper functions also read, and evaluate
helper calls before entering the lock.

## Supervision must follow the deployment, not a hardcoded path

A watchdog script ended in a hardcoded launcher path. When a new build was
deployed and later restarted by that watchdog, it silently relaunched the
**old** build and reported success. Every health check passed, because the
model was up — just not the one that had been deployed.

Read the thing to launch from state that the deployment updates. And when
verifying a deployment, check a number that actually differs between the two
builds — a memory pool size, a version string — not merely that something is
running.
