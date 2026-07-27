# Screenshots

Used by the README and the guides in `docs/`, and served to the dashboard's own
docs drawer at `/docs-images/<filename>`.

## What is real and what is not

**Node telemetry is real** — GPU, thermal, memory and storage readings came from actual
DGX Spark and ASUS GX10 hardware.

**The 28 days of history behind the trend, activity and cost charts is synthetic**,
generated with a plausible daily pattern so the charts have shape. So the token totals,
duty cycle, session list and cost figures in those images are illustrative, not
measurements. Node names, model names and client addresses are placeholders.

## Regenerating them

1. Run Spark Monitor with a config using generic `cluster_name` and node names.
2. Screenshot at 1500 px wide with a 2× device scale factor, dark theme.
3. Downscale to 1660 px wide (GitHub renders doc content at ~830 px, so this is 2×
   retina) and optimise.

Keep filenames stable — they are referenced from several documents, and
`.github/check_links.py` fails the build on a broken reference.

Check any new screenshot for hostnames, IP addresses, tailnet names and real model
names before committing. `.github/check_no_pii.py` scans text files, but it cannot read
what is inside a PNG.
