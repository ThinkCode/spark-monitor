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
    "argparse", "ast", "concurrent", "hmac", "http", "importlib",
    "ipaddress", "json", "os",
    "pathlib", "re", "shlex", "shutil", "struct", "subprocess", "sys", "tempfile",
    "threading", "time", "urllib", "zlib",
}

# Modules that live in this repository. Importing one is not a dependency.
LOCAL_MODULES = {p.stem for p in pathlib.Path(".").glob("*.py")}

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
            # A sibling module shipped in this repo is not a dependency: it is
            # this project. Only imports that would need installing count.
            if root and root in LOCAL_MODULES:
                continue
            if root and root not in ALLOWED:
                print(f"{path}:{node.lineno}: non-stdlib import {name!r}")
                failed = True

print("FAIL: dependency introduced" if failed
      else "ok: standard library only")
sys.exit(1 if failed else 0)
