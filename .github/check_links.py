#!/usr/bin/env python3
"""Fail if a markdown file references a relative path that does not exist.

Docs are also served in the dashboard's own drawer, so a broken link or image is
broken in the product, not just on GitHub.
"""
import pathlib
import re
import sys

# Markdown links and images, plus raw <img src> — the docs are markdown-only by
# preference, but a stray HTML tag's src is just as breakable.
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)|<img[^>]+src=[\"']([^\"']+)[\"']")
FENCE = re.compile(r"^\s*```")
CODE_SPAN = re.compile(r"`[^`]*`")

root = pathlib.Path(".")
failed = False

for md in sorted(root.rglob("*.md")):
    if ".git" in md.parts:
        continue
    in_fence = False
    for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Docs often show markup as an example, e.g. `![alt](src)`. Inline code
        # is prose about syntax, not a reference worth resolving.
        line = CODE_SPAN.sub("", line)
        for m in LINK.finditer(line):
            target = (m.group(1) or m.group(2)).split("#")[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (md.parent / target).resolve().exists():
                print(f"{md}:{lineno}: broken link -> {target}")
                failed = True

print("FAIL: broken links" if failed else "ok: all relative links resolve")
sys.exit(1 if failed else 0)
