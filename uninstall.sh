#!/usr/bin/env bash
# Uninstall Notification Manager for the current user.
#
#   ./uninstall.sh           remove the service and the app; KEEP your data
#   ./uninstall.sh --purge   also delete your data (all recorded notifications)
set -euo pipefail

APP=notification-manager
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
INSTALL_DIR="${NM_INSTALL_DIR:-$DATA_DIR/app}"
UNIT_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$APP.service"
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --purge)   PURGE=1 ;;
    -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         echo "Unknown option: $arg (try --help)" >&2; exit 1 ;;
  esac
done

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now "$APP" 2>/dev/null || true
fi
if [[ -f "$UNIT_FILE" ]]; then
  rm -f "$UNIT_FILE"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Removed service $UNIT_FILE"
fi

if [[ -d "$INSTALL_DIR" ]]; then
  # Only delete a folder that really is an install (guards against a bad NM_INSTALL_DIR)
  if [[ -f "$INSTALL_DIR/app.py" && -f "$INSTALL_DIR/templates/index.html" ]]; then
    rm -rf "$INSTALL_DIR"
    echo "Removed app $INSTALL_DIR"
  else
    echo "Not removing $INSTALL_DIR: it does not look like a Notification Manager install." >&2
  fi
fi

if (( PURGE )); then
  if [[ -d "$DATA_DIR" ]]; then
    read -r -p "Delete ALL recorded notifications in $DATA_DIR? [y/N] " answer
    case "${answer,,}" in
      y|yes) rm -rf "$DATA_DIR"; echo "Removed data $DATA_DIR" ;;
      *)     echo "Kept data in $DATA_DIR" ;;
    esac
  fi
elif [[ -d "$DATA_DIR" ]]; then
  echo "Your data is kept in $DATA_DIR (use --purge to delete it)."
fi
