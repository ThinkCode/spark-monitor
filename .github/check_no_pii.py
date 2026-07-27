#!/usr/bin/env python3
"""Fail if real-looking infrastructure details appear outside the allowed places.

This project is public. Committing a real node address, tailnet name or personal
hostname is a privacy leak that is easy to make and hard to undo once pushed, so
it is checked automatically.

Documentation examples deliberately use RFC 5737 / RFC 1918 documentation-style
addresses; those are allowed.
"""
import pathlib
import re
import sys

# Addresses that are fine to appear in docs and code.
ALLOWED_LITERALS = {
    "127.0.0.1", "0.0.0.0", "1.1.1.1", "255.255.255.0",
    # documentation examples used consistently across the docs
    "10.0.0.12", "10.0.0.13", "10.10.7.0", "10.10.7.1", "10.10.7.2",
    "10.10.8.1", "192.168.1.0", "192.168.1.42", "192.168.1.24",
    "100.101.102.103", "100.64.1.9",
}
# Patterns that should never appear at all.
FORBIDDEN = [
    (re.compile(r"\b100\.(?!101\.102\.103|64\.1\.9)\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
     "looks like a real Tailscale address"),
    (re.compile(r"\btail[0-9a-f]{6,}\.ts\.net\b"), "real tailnet name"),
    # TLD must not be numeric, or every "user@<ip-address>" SSH example trips it.
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"), "email address"),
]
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SKIP_DIRS = {".git", "__pycache__"}
CHECK_SUFFIX = {".md", ".py", ".sh", ".json", ".yml", ".yaml", ".service"}

failed = False
for path in sorted(pathlib.Path(".").rglob("*")):
    if not path.is_file() or set(path.parts) & SKIP_DIRS:
        continue
    if path.suffix not in CHECK_SUFFIX:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for lineno, line in enumerate(text.splitlines(), 1):
        for ip in IP.findall(line):
            if ip not in ALLOWED_LITERALS:
                print(f"{path}:{lineno}: unrecognised IP {ip} "
                      f"(add to ALLOWED_LITERALS if it is a doc example)")
                failed = True
        for pat, why in FORBIDDEN:
            m = pat.search(line)
            if m:
                print(f"{path}:{lineno}: {why}: {m.group(0)}")
                failed = True

print("FAIL: possible private data committed" if failed
      else "ok: no private addresses or hostnames found")
sys.exit(1 if failed else 0)
