#!/usr/bin/env bash
# install.sh — set Spark Monitor up as a service that survives reboots.
#
#   ./install.sh                 install and start
#   ./install.sh --uninstall     stop and remove the service (keeps your data)
#
# Installs a *user* systemd service, so this needs no root and touches nothing
# outside your home directory. `loginctl enable-linger` is what lets a user
# service keep running when you are not logged in — that is the one command
# here that may prompt for your password on some distributions.
set -euo pipefail

APP=spark-monitor
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$APP.service"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
CONFIG="$CONFIG_DIR/config.json"

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------- uninstall ----
if [[ "${1:-}" == "--uninstall" ]]; then
  step "Removing the $APP service"
  systemctl --user disable --now "$APP" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  say "service removed. Your config and history were left alone:"
  say "  config:  $CONFIG"
  say "  data:    ${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
  exit 0
fi

step "Checking prerequisites"
command -v python3 >/dev/null || die "python3 not found. Install it and re-run."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' \
  || die "Python 3.8+ required (found $PYV)."
say "python3 $PYV — no other dependencies needed"

[[ -f "$HERE/$APP.py" ]] || die "$APP.py not found next to install.sh"

if command -v nvidia-smi >/dev/null 2>&1; then
  say "nvidia-smi: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
else
  say "WARNING: nvidia-smi not found. GPU metrics will be blank."
  say "         Install it, or run this on the Spark itself rather than a laptop."
fi

if ! command -v systemctl >/dev/null 2>&1; then
  say "systemd not available — skipping the service."
  say "Run it in the foreground instead:  python3 $HERE/$APP.py"
  exit 0
fi

# --------------------------------------------------------------- config ----
step "Configuration"
if [[ -f "$CONFIG" ]]; then
  say "using the existing config: $CONFIG"
else
  mkdir -p "$CONFIG_DIR"
  python3 "$HERE/$APP.py" --write-config >/dev/null
  say "wrote a starter config for this machine: $CONFIG"
  say "It monitors this node only. To add more nodes, edit the \"nodes\" list"
  say "— see docs/MULTI-NODE.md."
fi
PORT=$(python3 -c "
import json,sys
try: print(json.load(open('$CONFIG')).get('port', 8088))
except Exception: print(8088)")

# ------------------------------------------------------------- service ----
step "Installing the systemd user service"
mkdir -p "$UNIT_DIR"
sed -e "s|@PYTHON@|$(command -v python3)|g" \
    -e "s|@SCRIPT@|$HERE/$APP.py|g" \
    -e "s|@WORKDIR@|$HERE|g" \
    "$HERE/contrib/$APP.service" > "$UNIT"
say "unit: $UNIT"

# A user service normally dies at logout. Lingering keeps it running, which is
# what makes the dashboard survive a reboot on a headless node.
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  say "enabling linger so the service runs without an active login"
  loginctl enable-linger "$USER" 2>/dev/null \
    || say "could not enable linger; run: sudo loginctl enable-linger $USER"
fi

systemctl --user daemon-reload
systemctl --user enable --now "$APP"

# ---------------------------------------------------------------- verify ----
step "Verifying"
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    OK=1; break
  fi
  sleep 0.5
done

if [[ "${OK:-}" != "1" ]]; then
  printf '\n'
  say "the service did not answer on port $PORT. Recent log:"
  journalctl --user -u "$APP" -n 25 --no-pager 2>/dev/null | sed 's/^/    /'
  die "see docs/TROUBLESHOOTING.md"
fi

IP=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || true)
printf '\n'
say "Spark Monitor is running and enabled at boot."
printf '\n'
say "Open it at:"
say "  http://localhost:$PORT"
[[ -n "$IP" ]] && say "  http://$IP:$PORT          (from another device on your LAN)"
if command -v tailscale >/dev/null 2>&1; then
  TS=$(tailscale ip -4 2>/dev/null | head -1 || true)
  [[ -n "$TS" ]] && say "  http://$TS:$PORT      (from anywhere, over your tailnet)"
else
  say "  For access from outside your LAN, see docs/TAILSCALE.md"
fi
printf '\n'
say "There is NO authentication. Never port-forward this port on your router."
printf '\n'
say "Useful commands:"
say "  systemctl --user status $APP        # is it running?"
say "  systemctl --user restart $APP       # after editing the config"
say "  journalctl --user -u $APP -f        # follow the log"
say "  python3 $APP.py --check             # probe every configured node"
