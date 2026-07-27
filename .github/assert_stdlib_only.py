#!/usr/bin/env python3
"""Fail the build if anything outside the standard library is imported.

Zero dependencies is the project's central promise (see CONTRIBUTING.md), so it
is enforced mechanically rather than by review. Parses the AST instead of
importing, so this works even on a machine where nothing is installed.
"""
import ast
import pathlib
import sys

# Modules this project may import. Everything here ships with CPython.
ALLOWED = {
    "argparse", "ast", "concurrent", "http", "importlib", "json", "os",
    "pathlib", "re", "shutil", "struct", "subprocess", "sys", "tempfile",
    "threading", "time", "urllib", "zlib",
}

failed = False
for path in sorted(pathlib.Path(".").glob("*.py")) + \
        sorted(pathlib.Path(".github").glob("*.py")):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            if root and root not in ALLOWED:
                print(f"{path}:{node.lineno}: non-stdlib import {name!r}")
                failed = True

print("FAIL: dependency introduced" if failed
      else "ok: standard library only")
sys.exit(1 if failed else 0)
