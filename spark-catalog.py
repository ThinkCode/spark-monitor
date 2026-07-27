#!/usr/bin/env python3
"""spark-catalog.py — inventory the model weights on a Spark Monitor cluster.

    ./spark-catalog.py                 # print the catalog as a table
    ./spark-catalog.py --json          # print it as JSON
    ./spark-catalog.py --write         # publish it for the dashboard drawer

Optional. Spark Monitor works fine without it; running it fills in the
dashboard's "Model Catalog" drawer.

It answers three questions:
  1. which models are on this cluster, and how big are they?
  2. where does each one live, and how many copies exist?
  3. which of those copies are REQUIRED, and which are just duplication?

That last distinction is the point. A model sharded across two nodes with
tensor parallelism MUST exist on both — that is not waste. The same model left
behind on a third node after you moved it is. List the former in
`catalog.pinned` so this tool stops reporting it as reclaimable.

Reads the same config file as spark-monitor.py: nodes, ssh_user, ssh_options
and serve_ports all come from there. See docs/CONFIGURATION.md.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Reuse the server's config loader so there is exactly one config format and
# one place that knows the defaults. The hyphen in the filename means it cannot
# be imported by name.
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("_sm", os.path.join(HERE, "spark-monitor.py"))
_sm = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_sm)

# Extra catalog-only settings, merged from the config file's "catalog" block.
CATALOG_DEFAULTS = {
    # Directories and globs searched on each node. `~` expands on the node.
    "scan": [
        "~/.cache/huggingface/hub",     # HF cache (models--org--Name layout)
        "~/models",                     # a directory per model
        "~/models/*.gguf",              # single-file quantised weights
        "~/gguf/*.gguf",
    ],
    # Optional shared storage (NAS, NFS mount) holding master copies. Set
    # "mount" to a path that exists on the nodes to enable the section.
    "shared_storage": {"mount": None, "sections": ["models", "backups"]},
    # Copies that are required rather than wasteful: {"substring": "why"}.
    "pinned": {},
}


def cfg_for(path=None):
    cfg = _sm.load_config(path)
    raw = {}
    if cfg["_existed"]:
        with open(cfg["_path"]) as f:
            raw = json.load(f)
    cat = dict(CATALOG_DEFAULTS)
    for k, v in (raw.get("catalog") or {}).items():
        if k in cat:
            cat[k] = ({**cat[k], **v} if isinstance(cat[k], dict)
                      and isinstance(v, dict) else v)
    cfg["catalog"] = cat
    return cfg


def build_probe(scan):
    """The shell script run on each node. Emits size<TAB>kind<TAB>name<TAB>path.

    Deliberately one script rather than many small commands: each SSH round
    trip costs more than the whole scan does.
    """
    dirs = [p for p in scan if not p.endswith(".gguf")]
    globs = [p for p in scan if p.endswith(".gguf")]
    s = ["emit() { printf '%s\\t%s\\t%s\\t%s\\n' \"$1\" \"$2\" \"$3\" \"$4\"; }"]
    for base in dirs:
        s.append(f'''
base={base}
if [ -d "$base" ]; then
  # HuggingFace cache layout: models--org--Name  ->  org/Name
  for d in "$base"/models--*; do
    [ -d "$d" ] || continue
    n=$(basename "$d" | sed 's/^models--//; s/--/\\//g')
    emit "$(du -sb "$d" 2>/dev/null | cut -f1)" hf "$n" "$d"
  done
  # plain directory-per-model
  for d in "$base"/*/; do
    [ -d "$d" ] || continue
    case "$(basename "$d")" in models--*|hub) continue;; esac
    # a directory only counts as a model if it holds weights
    ls "$d"*.safetensors "$d"*.bin "$d"*.gguf >/dev/null 2>&1 || continue
    emit "$(du -sb "$d" 2>/dev/null | cut -f1)" dir "$(basename "$d")" "${{d%/}}"
  done
fi''')
    for g in globs:
        s.append(f'''
for f in {g}; do
  [ -f "$f" ] || continue
  emit "$(stat -c %s "$f" 2>/dev/null)" gguf "$(basename "$f")" "$f"
done''')
    return "\n".join(s)


def build_serving(ports):
    """Ask each serving port on the node what it is currently serving."""
    plist = " ".join(str(p) for p in ports)
    return f'''
for p in {plist}; do
  curl -fsS --max-time 4 "http://127.0.0.1:$p/v1/models" 2>/dev/null \\
    | python3 -c 'import json,sys
try:
    for m in json.load(sys.stdin)["data"]: print(m["id"])
except Exception: pass' | sed "s/^/$p /"
done'''


def run_on(cfg, host, script, timeout=300):
    """Run a script on a node — locally when host is null, else over SSH."""
    if not host:
        cmd = ["bash", "-s"]
    else:
        target = f"{cfg['ssh_user']}@{host}" if cfg.get("ssh_user") else host
        cmd = ["ssh", *[str(o) for o in cfg["ssh_options"]], target, "bash -s"]
    try:
        r = subprocess.run(cmd, input=script, capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024


def probe_node(cfg, node):
    out = {"node": node["name"], "host": node["host"], "models": [],
           "serving": [], "reachable": False}
    probe = build_probe(cfg["catalog"]["scan"])
    for line in run_on(cfg, node["host"], probe).splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[0].isdigit():
            continue
        size, kind, name, path = parts
        out["reachable"] = True
        out["models"].append({"name": name, "kind": kind,
                              "bytes": int(size), "path": path})
    for line in run_on(cfg, node["host"],
                       build_serving(cfg["serve_ports"])).splitlines():
        bits = line.split(None, 1)
        if len(bits) == 2 and bits[0].isdigit():
            out["reachable"] = True
            out["serving"].append({"port": int(bits[0]), "model": bits[1]})
    return out


def shared_state(cfg, nodes):
    """Inspect the optional shared mount, via whichever node has it mounted.

    Asks every node in turn rather than a designated one: the catalog must not
    go blind because one node is down or its automount happens to be idle.
    """
    sc = cfg["catalog"]["shared_storage"]
    mount = sc.get("mount")
    if not mount:
        return {"configured": False, "mounted": False}
    sections = " ".join(sc.get("sections") or ["models"])
    script = f'''
mountpoint -q {mount} 2>/dev/null || [ -d {mount} ] || exit 1
df -B1 --output=size,used,avail {mount} 2>/dev/null | tail -1
for d in {sections}; do
  p={mount}/$d
  [ -d "$p" ] && printf 'SEC\\t%s\\t%s\\t%s\\n' "$d" \\
      "$(du -sb "$p" 2>/dev/null | cut -f1)" \\
      "$(find "$p" -type f 2>/dev/null | wc -l)"
done
# Every plausible identifier on the share -- directory names at a few depths
# plus weight filenames -- so the "backed up?" test is not fooled by layout.
for d in {mount}/*/ {mount}/*/*/ {mount}/*/*/*/; do
  [ -d "$d" ] && printf 'MOD\\t%s\\t%s\\n' "$(basename "$d")" \\
      "$(du -sb "$d" 2>/dev/null | cut -f1)"
done
find {mount} -maxdepth 5 -type f \\( -name '*.gguf' -o -name '*.safetensors' \\) \\
  2>/dev/null | while read -r f; do
    printf 'MOD\\t%s\\t%s\\n' "$(basename "$f")" "$(stat -c %s "$f" 2>/dev/null)"
  done'''
    for node in nodes:
        st = {"configured": True, "mounted": False, "mount": mount,
              "sections": [], "models": []}
        for line in run_on(cfg, node["host"], script).splitlines():
            f = line.split("\t")
            if f[0] == "SEC" and len(f) == 4:
                st["sections"].append({"name": f[1], "bytes": int(f[2] or 0),
                                       "files": int(f[3] or 0)})
            elif f[0] == "MOD" and len(f) == 3 and f[2].isdigit():
                st["models"].append({"name": f[1], "bytes": int(f[2])})
            elif re.fullmatch(r"\s*\d+\s+\d+\s+\d+\s*", line):
                size, used, avail = (int(x) for x in line.split())
                st.update(mounted=True, total_bytes=size, used_bytes=used,
                          avail_bytes=avail)
        if st["mounted"]:
            st["via"] = node["name"]
            return st
    return {"configured": True, "mounted": False, "mount": mount,
            "sections": [], "models": []}


def build(cfg):
    nodes_cfg = cfg["nodes"]
    with ThreadPoolExecutor(max_workers=max(1, len(nodes_cfg))) as ex:
        nodes = list(ex.map(lambda n: probe_node(cfg, n), nodes_cfg))
    shared = shared_state(cfg, nodes_cfg)
    shared_names = {m["name"].lower() for m in shared.get("models", [])}
    pinned = cfg["catalog"]["pinned"] or {}

    # Fold per-node findings into one entry per model name.
    models = {}
    for n in nodes:
        for m in n["models"]:
            e = models.setdefault(m["name"], {
                "name": m["name"], "kind": m["kind"], "bytes": 0,
                "locations": [], "served_as": []})
            e["bytes"] = max(e["bytes"], m["bytes"])
            e["locations"].append({"node": n["node"], "path": m["path"],
                                   "bytes": m["bytes"]})
    # Match served model ids back to weights on disk. Served names rarely equal
    # directory names, so compare on the first token of the short name.
    for n in nodes:
        for s in n["serving"]:
            for e in models.values():
                key = re.split(r"[-_.]", e["name"].split("/")[-1])[0].lower()
                if len(key) > 2 and key in s["model"].lower():
                    e["served_as"].append(f'{s["model"]} @ {n["node"]}')

    reclaimable = 0
    for e in models.values():
        e["copies"] = len(e["locations"])
        short = e["name"].split("/")[-1]
        cand = {short.lower(), short.replace("/", "--").lower(),
                ("models--" + e["name"].replace("/", "--")).lower()}
        e["on_shared"] = any(
            any(c == nm or (len(c) > 5 and (c in nm or nm in c)) for c in cand)
            for nm in shared_names)
        e["required_reason"] = next(
            (why for key, why in pinned.items() if key.lower() in e["name"].lower()),
            None)
        e["redundant_bytes"] = 0
        if e["copies"] > 1 and not e["required_reason"]:
            # keep the largest copy; the rest is what you could reclaim
            e["redundant_bytes"] = sum(
                sorted((l["bytes"] for l in e["locations"]), reverse=True)[1:])
            reclaimable += e["redundant_bytes"]

    return {
        "available": True,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # kept under the key the dashboard drawer reads
        "nas": shared,
        "nodes": [{"node": n["node"], "host": n["host"],
                   "reachable": n["reachable"], "serving": n["serving"],
                   "model_count": len(n["models"]),
                   "bytes": sum(m["bytes"] for m in n["models"])}
                  for n in nodes],
        "models": sorted(models.values(), key=lambda m: -m["bytes"]),
        "reclaimable_bytes": reclaimable,
    }


def render(c):
    L = [f"Model catalog   generated {c['generated']}"]
    n = c["nas"]
    if not n.get("configured"):
        L.append("Shared storage: not configured")
    elif n.get("mounted"):
        L.append(f"Shared storage {n['mount']} (via {n.get('via')}): "
                 f"{human(n.get('used_bytes'))} used of "
                 f"{human(n.get('total_bytes'))}, "
                 f"{human(n.get('avail_bytes'))} free")
        for s in n["sections"]:
            L.append(f"    {s['name']:<18}{human(s['bytes']):>10}  "
                     f"{s['files']:>5} files")
    else:
        L.append(f"Shared storage {n.get('mount')}: NOT MOUNTED on any node")

    unreachable = [x["node"] for x in c["nodes"] if not x["reachable"]]
    if unreachable:
        L.append(f"Unreachable, not counted: {', '.join(unreachable)}")

    L += ["", f"{'MODEL':<46}{'SIZE':>10}{'COPIES':>8}  {'SHARED':<8}WHERE",
          "-" * 118]
    for m in c["models"]:
        where = ", ".join(l["node"] for l in m["locations"])
        L.append(f"{m['name'][:45]:<46}{human(m['bytes']):>10}"
                 f"{m['copies']:>8}  {('yes' if m['on_shared'] else '-'):<8}{where}")
        pad = f"{'':<46}{'':>10}{'':>8}  {'':<8}"
        if m["served_as"]:
            L.append(pad + "serving: " + ", ".join(sorted(set(m["served_as"]))))
        if m["required_reason"]:
            L.append(pad + f"copies required: {m['required_reason']}")
        elif m["redundant_bytes"]:
            L.append(pad + f"redundant: {human(m['redundant_bytes'])}")

    if not c["models"]:
        L.append("(nothing found — check `catalog.scan` in your config)")

    L += ["", "SERVING NOW"]
    live = [(nd["node"], s) for nd in c["nodes"] for s in nd["serving"]]
    for name, s in live or []:
        L.append(f"    {name:<16}:{s['port']}  {s['model']}")
    if not live:
        L.append("    (no engine answered)")
    if c["reclaimable_bytes"]:
        L += ["", f"RECLAIMABLE (duplicate copies, excluding pinned ones): "
                  f"{human(c['reclaimable_bytes'])}"]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="spark-catalog",
        description="Inventory model weights across a Spark Monitor cluster.")
    ap.add_argument("--config", metavar="PATH", help="Spark Monitor config file")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--write", action="store_true",
                    help="publish to the dashboard's data dir")
    a = ap.parse_args(argv)

    cfg = cfg_for(a.config)
    if not cfg["_existed"]:
        print(f"note: no config at {cfg['_path']}; scanning this machine only",
              file=sys.stderr)
    cat = build(cfg)
    print(json.dumps(cat, indent=2) if a.json else render(cat))

    if a.write:
        dest = os.path.join(cfg["data_dir"], "catalog.json")
        os.makedirs(cfg["data_dir"], exist_ok=True)
        with open(dest, "w") as f:
            json.dump(cat, f, separators=(",", ":"))
        print(f"\npublished: {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
