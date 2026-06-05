#!/usr/bin/env bash
# Install pls and run it as a systemd daemon.
# Run with sudo like so:
#
# sudo bash scripts/install.sh [SOURCE_DIR]
#
# SOURCE_DIR defaults to the parent of this script (the checkout).
set -euo pipefail

SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

if [[ $EUID -ne 0 ]]; then
  echo "This script manages systemd; please re-run with sudo." >&2
  exit 1
fi

read -r -p "Install destination [/opt/pls]: " DEST
DEST="${DEST:-/opt/pls}"

echo "==> Installing pls from $SRC -> $DEST"

# 1. copy the tree, world-readable
mkdir -p "$DEST"
cp -rT "$SRC/server" "$DEST/server"
cp -rT "$SRC/client" "$DEST/client"
cp -rT "$SRC/bin"    "$DEST/bin"
install -m 0644 "$SRC/README.md" "$DEST/README.md" 2>/dev/null || true
# config: always ship the example; install the operator's .env but never clobber one already
# present (preserve edits across re-installs).
install -m 0644 "$SRC/.env.example" "$DEST/.env.example" 2>/dev/null || true
if [[ -f "$SRC/.env" && ! -f "$DEST/.env" ]]; then
  install -m 0644 "$SRC/.env" "$DEST/.env"
fi
chmod -R a+rX "$DEST"
chmod a+x "$DEST/bin/pls" "$DEST/server/app.py"

# 3. put the CLI on everyone's PATH
ln -sf "$DEST/bin/pls" /usr/local/bin/pls

# 4. install + start the daemon
install -m 0644 "$SRC/deploy/pls.service" /etc/systemd/system/pls.service
systemctl daemon-reload
systemctl enable --now pls

echo "==> Done."
echo "    health:  curl -s localhost:8555/healthz"
echo "    status:  pls status"
echo "    web:     http://localhost:8555/"
systemctl --no-pager --lines=0 status pls || true
