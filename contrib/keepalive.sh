#!/usr/bin/env bash
# keepalive.sh — start Spark Monitor if it is not already running.
#
# For systems without systemd. Add to crontab:
#
#   */5 * * * * $HOME/spark-monitor/contrib/keepalive.sh
#
# WHY THIS IS A SCRIPT AND NOT A ONE-LINE CRON JOB
#
# The obvious one-liner does not work:
#
#   */5 * * * * pgrep -f "spark-monitor[.]py" >/dev/null || python3 .../spark-monitor.py &
#
# `pgrep -f` matches against every process's full command line — including the
# shell cron started to run that very line. That shell's command line contains
# the text "spark-monitor.py" (it has to; it is the command being run), and the
# pattern matches it. So pgrep always reports a match, the `||` never fires, and
# the keepalive silently does nothing forever. It looks correct and it is a
# no-op — the worst kind of bug in a thing whose only job is recovery.
#
# Bracketing the pattern as "spark-monitor[.]py" does not help either: the
# bracket stops the pattern from matching *itself*, but the same command line
# still contains the unbracketed path being executed.
#
# Moving the logic into a script fixes it properly. This file's own command line
# is just its path, which does not contain "spark-monitor.py", so the check sees
# only the real server process. (pgrep already excludes itself.)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$HERE/spark-monitor.py"
LOG="${XDG_DATA_HOME:-$HOME/.local/share}/spark-monitor/keepalive.log"

# Match the interpreter too, not just the filename. `spark-monitor[.]py` alone
# also matches `vim spark-monitor.py` or `less spark-monitor.py` — so editing
# the file while the server is down would convince this check it is running,
# and it would never be restarted. Verified: the loose pattern matches an open
# editor; this one does not.
if pgrep -f -- "python3? -u .*spark-monitor\.py\$" >/dev/null 2>&1; then
  exit 0
fi

mkdir -p "$(dirname "$LOG")"
printf '%s starting spark-monitor\n' "$(date -Is)" >> "$LOG"

# setsid + </dev/null is required: a plain `nohup ... &` from a non-interactive
# shell dies when that shell exits, which is exactly what cron does immediately.
cd "$HERE" || exit 1
setsid nohup python3 -u "$SCRIPT" </dev/null >> "$LOG" 2>&1 &
