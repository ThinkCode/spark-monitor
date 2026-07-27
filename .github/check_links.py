#!/usr/bin/env python3
"""Fail if a markdown file links to a relative path that does not exist.

Docs are also served in the dashboard's own drawer, so a broken link is a
broken link in the product, not just on GitHub.
"""
import pathlib
import re
import sys

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
root = pathlib.Path(".")
failed = False

for md in sorted(root.rglob("*.md")):
    if ".git" in md.parts:
        continue
    for m in LINK.finditer(md.read_text(encoding="utf-8")):
        target = m.group(1).split("#")[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (md.parent / target).resolve().exists():
            print(f"{md}: broken link -> {target}")
            failed = True

print("FAIL: broken links" if failed else "ok: all relative links resolve")
sys.exit(1 if failed else 0)
