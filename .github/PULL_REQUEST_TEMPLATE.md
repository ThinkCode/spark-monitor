## What and why

<!-- What changes, and what problem it solves. If it fixes a bug, describe the
     wrong behaviour. -->

## How it was verified

Spark Monitor has no unit tests on purpose — its job is parsing real output from
real hardware. Please say what you actually ran it on.

- **Hardware:**  <!-- e.g. 2x DGX Spark -->
- **OS / Python:**  <!-- e.g. DGX OS 6, Python 3.10 -->
- **Engine:**  <!-- e.g. vLLM 0.11, or n/a -->

```
# paste: python3 spark-monitor.py --check     (redact addresses)
```

## Checklist

- [ ] `python3 -m py_compile spark-monitor.py` passes
- [ ] **No new dependencies** — standard library only, no CDN scripts or fonts
- [ ] Ran on real hardware, not just a syntax check
- [ ] Checked in **both light and dark** themes, at phone and desktop widths
- [ ] Checked with a node deliberately offline (should degrade, not break)
- [ ] Docs in `docs/` updated in this PR if behaviour or config changed
- [ ] `CHANGELOG.md` entry added under "Unreleased"
- [ ] No real config, hostname, IP address or history file in the diff

## Anything reviewers should know?

<!-- Trade-offs, things you weren't sure about, follow-up work. -->
