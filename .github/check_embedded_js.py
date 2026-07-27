#!/usr/bin/env python3
"""Syntax-check the JavaScript embedded in spark-monitor.py's HTML string.

The UI is one big Python string, so a JS syntax error is invisible to Python:
the file imports, the server starts, the page returns 200 — and nothing works,
because the entire script block failed to parse. No other check catches this.

The trap that motivated it: a block comment describing a cron schedule contained
a slash-star sequence, which CLOSED the comment early and turned the rest of the
line into stray code. The dashboard silently stopped working.

Uses `node --check`, which is a real parser — hand-rolled brace counting gives
false positives on regex literals like /\\*([^*]+)\\*/g. node is present on CI
runners; locally the check skips if it is missing.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

src = pathlib.Path("spark-monitor.py").read_text(encoding="utf-8")
try:
    html = src.split('HTML = r"""', 1)[1].split('"""', 1)[0]
except IndexError:
    sys.exit("could not find the HTML string in spark-monitor.py")

blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
if not blocks:
    sys.exit("no <script> blocks found — did the HTML change shape?")

if not shutil.which("node"):
    print(f"ok (skipped): node not installed; {len(blocks)} script block(s) unchecked")
    sys.exit(0)

failed = False
for i, blk in enumerate(blocks):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(blk)
        path = f.name
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    if r.returncode != 0:
        failed = True
        print(f"  script block {i + 1} does not parse:")
        for line in (r.stderr or "").strip().splitlines()[:8]:
            print(f"    {line}")
    pathlib.Path(path).unlink(missing_ok=True)

print("FAIL: embedded JavaScript has a syntax error" if failed
      else f"ok: {len(blocks)} embedded script block(s) parse cleanly")
sys.exit(1 if failed else 0)
