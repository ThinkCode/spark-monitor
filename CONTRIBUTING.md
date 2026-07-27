# Contributing

Thanks for considering it. This is a small, opinionated project, and knowing the
opinions up front will save you wasted effort.

## The most useful thing you can do

**Tell us what hardware it works on.** Spark Monitor was developed on DGX Sparks and
ASUS Ascent GX10s. If you run it on something else — a different GB10 variant, a
different distribution, a different inference engine — open an issue saying what worked
and what didn't, with the output of:

```bash
python3 spark-monitor.py --check
```

(Redact your addresses first.) That's genuinely valuable and takes two minutes.

## The one rule: zero dependencies

**`spark-monitor.py` imports only the Python standard library, and always will.**

Not "prefers to". Will. It's the property that makes the tool dependable: nothing to
install, nothing to keep patched, nothing that breaks when a package cuts a major
release, and it runs on a machine with no internet access.

That applies to the frontend too — no CDN scripts, no external fonts, no charting
library. If a feature needs `pip install` or a `<script src="https://...">`, the answer is
no, however good the feature is. Something can usually be built with what's already
there; if not, it belongs in a separate tool that talks to [`/api/stats`](docs/API.md).

Read [ARCHITECTURE.md](docs/ARCHITECTURE.md#deliberately-not-done) before proposing
anything structural — several tempting ideas are listed there with the reasons they were
rejected.

## Before you write code

**Open an issue first** for anything beyond a bug fix. It's a single-file project, so two
people editing the same area conflict badly, and it saves you building something that
will be turned down on principle.

Good candidates, if you want a starting point:

- Memory-bandwidth sampling via `nvidia-smi dmon`
- Network and storage I/O rates per node
- Support for an engine whose metrics aren't picked up yet
- A CSV/JSON export of a history window

## Style

Match the surrounding code. Specifically:

- **PEP 8**, 4-space indent, ~88 column soft limit.
- **Comments explain *why*, not *what*.** The codebase is full of comments recording why
  something is done a surprising way, or which obvious approach was tried and failed.
  Those comments are the most valuable thing in the file — a future reader who doesn't
  know a path is a dead end will walk down it again. If you discover a dead end, leave a
  note.
- **Collectors must degrade, never raise.** A parse of command output should return a
  default on failure, not propagate. One unreachable node must never break a poll.
- **Catch specific exceptions.** `except (OSError, ValueError)`, not bare `except`, unless
  you're wrapping a shell call where genuinely anything can happen — and say so if you are.
- **Frontend:** plain JS, no framework, no build step. Colours come from CSS custom
  properties so both themes work; never hardcode a hex value in a chart.

## Honesty about numbers

This matters more here than in most projects. Several dashboard numbers are estimates or
have caveats, and **the UI says so, in the UI, where the number is**.

If you add a metric that is modelled, inferred, or unavailable on some engines, label it
in the interface. Don't present a since-boot subtotal as a lifetime total. Don't sum
things whose sum is meaningless (see prompt vs generated tokens). A dashboard that quietly
misleads is worse than one that admits a gap.

## Testing

There's no test suite, and mocks wouldn't tell you much: the code's job is parsing real
command output from real hardware. So verify for real.

```bash
python3 -m py_compile spark-monitor.py
python3 spark-monitor.py --check
python3 spark-monitor.py --port 18099 --bind 127.0.0.1   # runs alongside the real one
```

Then check, in the browser:

- Both light and dark themes
- Phone width and desktop width
- With a node deliberately switched off — it should show "unreachable" and break nothing
- Every endpoint you touched

**Say in the pull request what you tested on**: node count, distribution, engine, Python
version. "Verified on 2× DGX Spark, DGX OS, vLLM 0.11, Python 3.10" is exactly right.

## Pull requests

- One change per PR.
- Explain **why**, not just what. If it fixes a bug, describe the wrong behaviour.
- Update the relevant doc in `docs/` in the same PR. A config key added without a
  `CONFIGURATION.md` entry is incomplete.
- Add a `CHANGELOG.md` entry under "Unreleased".
- **Never commit a real config, hostname, IP address or history file.** `.gitignore`
  covers the usual files; check your diff anyway.

## Reporting bugs

Use the issue template, and include:

```bash
python3 spark-monitor.py --check 2>&1
journalctl --user -u spark-monitor -n 50 --no-pager
python3 --version && uname -a
```

**Redact addresses and hostnames** — `--check` prints them. Say what you expected, what
happened, and whether it worked before.

## Security issues

Don't open a public issue. See [SECURITY.md](SECURITY.md#filing-a-report).

## Code of conduct

Be decent to people. Assume good faith, keep criticism about the code, and remember that
everyone here is doing this in their spare time. Behaviour that makes the project worse
to participate in gets you removed from it.

## License

Contributions are licensed under [MIT](LICENSE), the same as the project.
