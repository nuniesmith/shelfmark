#!/usr/bin/env bash
#
# Create the restricted account Freddy uses to pull completed downloads off
# Sullivan. Run ON SULLIVAN, with sudo:
#
#     sudo bash provision-sullivan-sync.sh "ssh-ed25519 AAAA... shelfmark@freddy"
#
# The public key argument is the one generated on Freddy (see docs/INVENTORY.md).
# The private half never leaves Freddy.
#
# What this account can do, and nothing else:
#   * rsync, READ ONLY, restricted to one directory
#   * no shell, no pty, no port forwarding, no agent forwarding
#
# The download category deliberately lives OUTSIDE /media/qbittorrent/complete.
# Unpackerr's catch-all watcher is UN_FOLDER_0_PATH=/complete, so a category
# under there would be extracted by Unpackerr and by Shelfmark at the same time.
# Keeping it out of that tree removes the race by construction rather than by
# configuration that can drift back.
set -euo pipefail

PUBKEY="${1:-}"
USER_NAME=shelfmark-sync
CATEGORY_DIR=/media/qbittorrent/shelfmark

if [ -z "$PUBKEY" ]; then
    echo "usage: sudo bash $0 \"ssh-ed25519 AAAA... comment\"" >&2
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root" >&2
    exit 2
fi
case "$PUBKEY" in
    ssh-ed25519\ *|ssh-rsa\ *) : ;;
    *) echo "that does not look like an SSH public key" >&2; exit 2 ;;
esac
if [ ! -x /usr/bin/rrsync ]; then
    echo "/usr/bin/rrsync is missing — install rsync's scripts package" >&2
    exit 2
fi

echo "▸ download category: $CATEGORY_DIR"
install -d -o actions -g actions -m 2775 "$CATEGORY_DIR"

echo "▸ account: $USER_NAME"
if ! id "$USER_NAME" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/$USER_NAME" \
            --shell /usr/sbin/nologin "$USER_NAME"
    echo "  created"
else
    echo "  already exists"
fi

# Read access to the category comes from the group that owns it, so no
# ownership of qBittorrent's files changes hands.
usermod -aG actions "$USER_NAME"

echo "▸ authorized_keys (forced read-only rrsync, single directory)"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 "/home/$USER_NAME/.ssh"
cat > "/home/$USER_NAME/.ssh/authorized_keys" <<EOF
command="/usr/bin/rrsync -ro $CATEGORY_DIR",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding $PUBKEY
EOF
chown "$USER_NAME:$USER_NAME" "/home/$USER_NAME/.ssh/authorized_keys"
chmod 600 "/home/$USER_NAME/.ssh/authorized_keys"

echo
echo "✔ done. Verify from Freddy — the first must work, the rest must all fail:"
echo "    rsync -n shelfmark-sync@100.87.125.19:/ /tmp/          # lists the category"
echo "    ssh shelfmark-sync@100.87.125.19 'id'                  # refused: no shell"
echo "    rsync -n /etc/hostname shelfmark-sync@100.87.125.19:/  # refused: read-only"
echo "    rsync -n shelfmark-sync@100.87.125.19:/etc/passwd /tmp/ # refused: outside the root"
