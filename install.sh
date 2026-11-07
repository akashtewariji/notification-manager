#!/usr/bin/env bash
# Install (or update) Notification Manager for the current user.
#
#   ./install.sh                 install, enable and start the systemd user service
#   ./install.sh --no-service    install only; run it yourself with
#                                <install dir>/.venv/bin/python <install dir>/app.py
#
# Environment overrides:
#   NM_PORT         port for the web UI          (default 5000)
#   NM_INSTALL_DIR  where the code + venv go     (default ~/.local/share/notification-manager/app)
#
# Your data (database, wallpaper, token) lives in ~/.local/share/notification-manager
# and is never touched by install or update.
set -euo pipefail

APP=notification-manager
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_DIR="${NM_INSTALL_DIR:-$DATA_HOME/$APP/app}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/$APP.service"
PORT="${NM_PORT:-5000}"
WITH_SERVICE=1

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✔]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✘]${NC} $*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --no-service) WITH_SERVICE=0 ;;
    -h|--help)    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            die "Unknown option: $arg (try --help)" ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] || die "NM_PORT must be a number, got: $PORT"
[[ "$INSTALL_DIR" != *[[:space:]\|]* ]] || die "The install path must not contain spaces or '|': $INSTALL_DIR"
[[ -f "$SRC_DIR/app.py" && -f "$SRC_DIR/templates/index.html" ]] \
  || die "Run this script from the repository folder (app.py not found next to it)."

# ── Prerequisites ───────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install it, e.g.: sudo apt install python3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "Python 3.10 or newer is required (found $(python3 -V 2>&1))."
python3 -m venv --help >/dev/null 2>&1 || die "python3-venv not found. e.g.: sudo apt install python3-venv"
command -v dbus-monitor >/dev/null 2>&1 || die "dbus-monitor not found. e.g.: sudo apt install dbus  (Fedora: dbus-tools)"
if (( WITH_SERVICE )); then
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found. Use --no-service and start the app yourself."
  systemctl --user show-environment >/dev/null 2>&1 \
    || die "No systemd user session available. Use --no-service and start the app yourself."
fi

# ── Copy the app ────────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/templates"
install -m 644 "$SRC_DIR/app.py"               "$INSTALL_DIR/app.py"
install -m 644 "$SRC_DIR/requirements.txt"     "$INSTALL_DIR/requirements.txt"
install -m 644 "$SRC_DIR/templates/index.html" "$INSTALL_DIR/templates/index.html"
info "Copied app to $INSTALL_DIR"

# ── Python environment ─────────────────────────────────────────────────────
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade -r "$INSTALL_DIR/requirements.txt"
info "Python dependencies installed"

if (( ! WITH_SERVICE )); then
  echo
  info "Installed without a service. Start it with:"
  echo "    NM_PORT=$PORT $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/app.py"
  echo "  then open http://localhost:$PORT"
  exit 0
fi

# ── systemd user service ───────────────────────────────────────────────────
if command -v ss >/dev/null 2>&1 && ! systemctl --user -q is-active "$APP" 2>/dev/null \
   && ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
  warn "Port $PORT is already in use. Re-run with NM_PORT=<free port> ./install.sh"
fi

mkdir -p "$UNIT_DIR"
sed -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" -e "s|@PORT@|$PORT|g" \
  "$SRC_DIR/notification-manager.service" > "$UNIT_FILE"
systemctl --user daemon-reload
systemctl --user enable -q "$APP"
systemctl --user restart "$APP"
info "Service installed: $UNIT_FILE"

sleep 2
if systemctl --user -q is-active "$APP"; then
  echo
  info "Notification Manager is running → http://localhost:$PORT"
  echo "    It starts automatically with your desktop session."
  echo "    Logs:      journalctl --user -u $APP -f"
  echo "    Uninstall: $SRC_DIR/uninstall.sh"
else
  die "Service failed to start. See: journalctl --user -u $APP -n 50"
fi
