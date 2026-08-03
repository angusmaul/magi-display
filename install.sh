#!/bin/sh
# MAGI display installer — https://github.com/angusmaul/magi-display
#
# Installs the panel, the collector and two systemd units onto the box that
# drives the screen. Run it as the user the display session belongs to; it
# calls sudo only for the parts under /etc and /etc/systemd.
#
#   ./install.sh [rack|console]
#
# Defaults to `console`. The difference is only which page the web server
# serves as index and what the launcher points at — both panels read the same
# data.json.
set -eu

PANEL="${1:-console}"
case "$PANEL" in
  rack)    PAGE=index.html ;;
  console) PAGE=console.html ;;
  *) echo "usage: $0 [rack|console]" >&2; exit 2 ;;
esac

SRC="$(cd "$(dirname "$0")" && pwd)"
MAGI_HOME="${MAGI_HOME:-$HOME/magi}"
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"

echo "==> installing $PANEL panel ($PAGE) to $MAGI_HOME"
mkdir -p "$MAGI_HOME/display"
install -m 644 "$SRC/display/$PAGE" "$MAGI_HOME/display/$PAGE"
install -m 755 "$SRC/collector/magi_collect.py" "$MAGI_HOME/magi_collect.py"

# Files edited on Windows carry CRLF, which breaks both shell scripts and
# systemd EnvironmentFile values in ways that are painful to diagnose.
sed -i 's/\r$//' "$MAGI_HOME/display/$PAGE" "$MAGI_HOME/magi_collect.py" 2>/dev/null || true

# Provenance stamp. Without this there is no way to ask a running panel where
# its files came from — and a panel you cannot ask is a panel that quietly
# drifts, because the safe-feeling move is always to edit it in place rather
# than risk reinstalling over something you cannot diff. `magi-verify` reads
# this. Records the source revision, not just a date, so "is this box current"
# is answerable rather than a guess.
echo "==> stamping provenance"
_rev="unknown"; _dirty=""
if command -v git >/dev/null 2>&1 && git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
  _rev="$(git -C "$SRC" rev-parse HEAD)"
  # A dirty tree is worth recording loudly: it means the deployed bytes exist
  # nowhere in history and cannot be reproduced from the revision alone.
  git -C "$SRC" diff --quiet HEAD -- collector display 2>/dev/null || _dirty=" (dirty)"
fi
cat > "$MAGI_HOME/INSTALLED" <<STAMPEOF
# Written by install.sh. Do not edit — regenerate by reinstalling.
panel=$PANEL
page=$PAGE
revision=$_rev$_dirty
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
installed_by=$USER_NAME@$(hostname)
source=$SRC
STAMPEOF
chmod 644 "$MAGI_HOME/INSTALLED"
echo "    revision $_rev$_dirty"

echo "==> installing fonts"
mkdir -p "$HOME/.local/share/fonts/michroma"
install -m 644 "$SRC/fonts/Michroma-Regular.ttf" "$SRC/fonts/OFL.txt" \
  "$HOME/.local/share/fonts/michroma/"
fc-cache -f "$HOME/.local/share/fonts" >/dev/null 2>&1 || true

echo "==> installing systemd units"
for unit in magi-collect magi-web; do
  sed -e "s|__USER__|$USER_NAME|g" \
      -e "s|__GROUP__|$GROUP_NAME|g" \
      -e "s|__MAGI_HOME__|$MAGI_HOME|g" \
      -e "s|__PAGE__|$PAGE|g" \
      "$SRC/systemd/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done

echo "==> creating /etc/magi"
sudo install -d -m 755 /etc/magi
if [ ! -f /etc/magi/magi.env ]; then
  sudo tee /etc/magi/magi.env >/dev/null <<'ENVEOF'
# Proxmox API. Create a READ-ONLY token first:
#   pveum user token add root@pam magi-display --privsep 1
#   pveum acl modify / --tokens 'root@pam!magi-display' --roles PVEAuditor
PVE_HOST=10.0.0.10
PVE_TOKEN_ID=root@pam!magi-display
PVE_TOKEN_SECRET=CHANGE-ME
PVE_CA=/etc/magi/pve-root-ca.pem

# Console panel only: guest counts + quorum state (2 extra API calls per tick).
#MAGI_EXTRA=1

# Console panel only: an Uptime Kuma *published* status page. No token needed.
#MAGI_KUMA_URL=http://10.0.0.20:3001
#MAGI_KUMA_SLUG=main
ENVEOF
  sudo chmod 600 /etc/magi/magi.env
  sudo chown root:root /etc/magi/magi.env
  echo "    wrote /etc/magi/magi.env — EDIT IT, then:"
else
  echo "    /etc/magi/magi.env already exists, left alone"
fi

cat <<NEXT

Next:
  1. Copy your PVE cluster CA to /etc/magi/pve-root-ca.pem
       scp root@<pve-node>:/etc/pve/pve-root-ca.pem /tmp/ && \\
       sudo install -m 644 /tmp/pve-root-ca.pem /etc/magi/
  2. Edit /etc/magi/magi.env with your host and token secret.
  3. Edit the UNITS array at the top of the <script> block in
     $MAGI_HOME/display/$PAGE so the node names match your cluster.
  4. sudo systemctl daemon-reload
     sudo systemctl enable --now magi-web magi-collect
  5. Point a browser at http://127.0.0.1:8080/$PAGE
     (see kiosk/autostart for an unattended example)

NEXT
