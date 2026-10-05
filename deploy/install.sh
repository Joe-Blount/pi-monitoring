#!/usr/bin/env bash
# Install this node's monitoring. Run as root on the machine itself.
#
#   sudo deploy/install.sh <site> <node>
#   sudo deploy/install.sh garage garage
#   sudo deploy/install.sh blind1 upstairs
#
# Idempotent: safe to run again after a git pull, which is how updates happen.

set -euo pipefail

SITE="${1:-}"
NODE="${2:-}"
FORCE="${3:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_FILE="$REPO/sites/$SITE/$NODE.yaml"

if [[ -z "$SITE" || -z "$NODE" ]]; then
    echo "usage: $0 <site> <node> [--force]" >&2
    echo "available:" >&2
    find "$REPO/sites" -name '*.yaml' | sed "s|$REPO/sites/|  |; s|/| |; s|\.yaml$||" >&2
    exit 2
fi
if [[ ! -f "$NODE_FILE" ]]; then
    echo "no such node file: $NODE_FILE" >&2
    exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
    echo "run this as root" >&2
    exit 2
fi

echo "==> packages"
# python3-yaml is the only hard requirement. Drivers that need more pull their
# own packages in as they are written, so a sensors-only node stays small.
apt-get install -y --no-install-recommends python3-yaml >/dev/null
echo "    python3-yaml present"

echo "==> directories"
install -d -m 0755 /etc/monitoring
install -d -m 0755 /var/lib/monitoring
# State files are written by whatever runs the collectors. telegraf needs to be
# able to keep a rain total or a lockout counter across a restart.
chgrp telegraf /var/lib/monitoring 2>/dev/null || true
chmod g+w /var/lib/monitoring 2>/dev/null || true

echo "==> node file"
ln -sfn "$NODE_FILE" /etc/monitoring/node.yaml
echo "    /etc/monitoring/node.yaml -> $NODE_FILE"

echo "==> code"
if [[ "$REPO" != "/opt/monitoring" ]]; then
    ln -sfn "$REPO" /opt/monitoring
    echo "    /opt/monitoring -> $REPO"
fi

echo "==> credentials"
if [[ -f /etc/monitoring/influx.env ]]; then
    echo "    /etc/monitoring/influx.env already exists, left untouched"
else
    echo "    no /etc/monitoring/influx.env yet."
    echo "    This machine may already carry its token elsewhere, such as"
    echo "    /etc/default/telegraf. If so, nothing to do. Otherwise see"
    echo "    influx.env.example for how to install one without it reaching"
    echo "    a command line or shell history."
fi

echo "==> group membership"
# Reading a GPIO, an I2C bus or a serial port needs the group, and the failure
# without it is a permission error that reads like a missing device.
for group in gpio i2c dialout bluetooth; do
    if getent group "$group" >/dev/null; then
        usermod -aG "$group" telegraf 2>/dev/null && echo "    telegraf added to $group" || true
    fi
done

echo "==> telegraf input"
install -d -m 0755 /etc/telegraf/telegraf.d
install -m 0644 "$REPO/deploy/telegraf.d/monitoring.conf" /etc/telegraf/telegraf.d/monitoring.conf
echo "    /etc/telegraf/telegraf.d/monitoring.conf installed (input only; the"
echo "    output on this machine is left exactly as it was)"

echo "==> checking devices"
# A warning, never a refusal. A dead five dollar sensor must not block a
# software update at a site nobody can reach; it fails loudly at runtime.
if ! /opt/monitoring/bin/collect --node /etc/monitoring/node.yaml --check; then
    if [[ "$FORCE" == "--force" ]]; then
        echo "    continuing anyway (--force)"
    else
        echo "    some devices failed. Continuing: this is a warning."
    fi
fi

echo "==> restarting telegraf"
systemctl restart telegraf
sleep 3
systemctl is-active --quiet telegraf && echo "    telegraf is running" || {
    echo "    telegraf did NOT start. Check: journalctl -u telegraf -n 30" >&2
    exit 1
}

echo
echo "Installed $SITE/$NODE."
echo "Watch it with:  journalctl -u telegraf -f"
echo "Test by hand:   /opt/monitoring/bin/collect --node /etc/monitoring/node.yaml --poll"
