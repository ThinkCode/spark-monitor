# Firmware and versions

The Firmware drawer answers two questions for every node: **what is it
running**, and **is anything behind**. It never installs anything.

Open it from the button beside Model Catalog. The button glows with a count
when something needs attention.

## What it reports

| | source | notes |
|---|---|---|
| BIOS / UEFI version and date | DMI (`/sys/class/dmi/id`) | no root needed |
| Embedded controller, NIC, NVMe, UEFI firmware | `fwupdmgr get-devices` | every updatable device |
| Pending firmware updates | `fwupdmgr get-updates` | what LVFS or the vendor publishes |
| NVIDIA driver | `nvidia-smi` | |
| Kernel | `uname -r` | |
| Driver / kernel / firmware packages | `apt list --upgradable` | see the caveat below |
| BIOS drift | this cluster | see below |

Everything is read-only and unprivileged. `fwupdmgr get-devices` and
`get-updates` work fine as a normal user; only installing needs root.

## "Is there a newer version?" — where each answer comes from

**Device firmware** is authoritative. `fwupd` talks to LVFS or the vendor, so
an update shown here is one the vendor actually published.

**Packages** are as fresh as the apt index, and refreshing that index needs
root — which this dashboard does not have. The drawer therefore prints how old
the index is (`apt index 6h old`). If that number is large, the answer is stale
and the UI says so rather than implying it is current.

**BIOS has no public "latest" feed.** Nobody publishes a machine-readable
"newest DGX Spark BIOS" endpoint, so instead the cluster is its own reference:
among nodes reporting the same `product_name`, any node behind the newest BIOS
observed is flagged as **drift**:

```
BIOS GX10DGX.0104.2026.0326.1657  ← behind GX10DGX.0105.2026.0505.1153 on an identical node
```

That is deliberately called drift and not "update available". "One node is
behind another" is all this can honestly know. It is still worth surfacing:
mixed firmware across otherwise identical nodes is a real source of
hard-to-reproduce behaviour.

## Why it does not install anything

Every update path here needs root — `fwupdmgr update`, `apt full-upgrade`,
`reboot` — and the dashboard deliberately runs unprivileged with a single
write endpoint. But the deeper reason is blast radius:

- A topology or model change is reversible. Restart something and you are back.
- **A failed UEFI or embedded-controller capsule can leave a machine that does
  not boot.** No amount of orchestration recovers that remotely; it is recovery
  media and physical access.

So the drawer generates the exact commands for the node in front of you, from
what is actually pending on it, and stops:

```
ssh user@node
sudo fwupdmgr refresh --force
sudo fwupdmgr update          # 3 device(s)
sudo apt update && sudo apt full-upgrade
sudo reboot                   # required: kernel update, firmware capsule
```

Nodes with no capsule and no kernel change are labelled *packages only, no
reboot* rather than told to stop their workloads — a warning that fires on
everything is a warning people learn to ignore.

## Applying updates safely

Learned from doing this on a live cluster:

**Stop whatever is running on the node first.** Obvious for a single-node
model. Less obvious for a tensor-parallel one: a TP=4 model spans four nodes
and cannot survive losing any of them, so servicing one ring node means
stopping the whole model. There is no rolling firmware update for a
tensor-parallel ring.

**Do your least critical node first.** If two nodes take the same capsules,
flashing the one that is not carrying the critical workload first costs
nothing and turns the second flash into a known quantity.

**A capsule reboot is much longer than a normal one.** Measured on two GX10s:
**12 minutes** of total unreachability each. The capsule is applied by firmware
*before* the kernel starts, so when the node returns, its `uptime` will read
only a few minutes — do not mistake that for the length of the outage. Budget
15 minutes per node with capsules; a kernel-only reboot is 1–2 minutes.

**Do not power-cycle a slow boot.** A half-written embedded-controller capsule
is precisely how these boards are bricked. Wait it out.

**Retry a failed capsule once before concluding anything.** A real example:

```
Update State:  Failed
Update Error:  failed to run update on reboot: expected 0x00000516 and got 0x00000001
```

The identical device on an identical node had succeeded on its first attempt,
the ESP had 500 MB free and Secure Boot was on — neither space nor signing. A
plain `sudo fwupdmgr update` and a second reboot applied it. Check
`fwupdmgr get-history` for the real state; a device silently sitting at its old
version is not always reported as pending.

**Verify against what is running, not what is staged.** Some devices report a
staged version separately from the live one. Re-open the drawer after the
reboot and confirm the version actually moved.

## Reporting versions the dashboard cannot reach itself

Set `firmware_report` in your config to a JSON file path. Anything you like can
write it — a cron job, a script on your laptop, an agent — and the drawer will
render it under **Reported externally** with its own staleness marker.

```json
{
  "items": [
    { "name": "Fabric switch", "version": "7.20.8", "latest": "7.23.3",
      "update": true,
      "note": "RouterBOOT upgrades separately from the OS and needs a reboot",
      "commands": ["/system/routerboard/upgrade", "/system/reboot"] }
  ]
}
```

The dashboard **reads** this file and never writes it, which keeps the
single-write-endpoint property of the server intact. Add a `ts` field (epoch
seconds) and anything older than an hour is flagged stale, so a reporter that
has stopped running is visible instead of quietly showing old versions as
current.

This is the intended way to cover a managed switch, a UPS, or a tool installed
somewhere other than the cluster.
