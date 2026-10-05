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
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_FILE="$REPO/sites/$SITE/$NODE.yaml"

if [[ -z "$SITE" || -z "$NODE" ]]; then
    echo "usage: $0 <site> <node>" >&2
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
# Bluetooth drivers need bleak, which is packaged on both releases in use, so
# it installs like everything else rather than needing a virtual environment.
# Only when this node actually declares a Bluetooth device: a sensors-only
# machine should not carry a Bluetooth stack it never uses.
if grep -qE "driver:\s*ble_" "$NODE_FILE"; then
    if python3 -c "import bleak" 2>/dev/null; then
        echo "    python3-bleak already present"
    else
        apt-get install -y --no-install-recommends python3-bleak >/dev/null \
            || echo "    WARNING: could not install python3-bleak; Bluetooth devices will fail"
    fi
fi

if python3 -c "import yaml" 2>/dev/null; then
    echo "    python3-yaml already present"
else
    # Deliberately not fatal: a site with no uplink today must still be able
    # to take a code update.
    apt-get install -y --no-install-recommends python3-yaml >/dev/null \
        || echo "    WARNING: could not install python3-yaml. Nothing will run"
fi

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
    if [[ -e /opt/monitoring && ! -L /opt/monitoring ]]; then
        echo "    /opt/monitoring exists and is not a link. Refusing: linking" >&2
        echo "    into it would put the link inside the directory." >&2
        exit 2
    fi
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

echo "==> telegraf inputs"
install -d -m 0755 /etc/telegraf/telegraf.d

# Remove this project's previous fragments first. Without that, a device
# deleted from a node file would leave its stanza behind and telegraf would
# keep running a collector for hardware that is no longer declared.
rm -f /etc/telegraf/telegraf.d/pi-monitoring.conf
rm -f /etc/telegraf/telegraf.d/pi-monitoring-*.conf
# The old generic names, removed once so an existing installation is tidied.
rm -f /etc/telegraf/telegraf.d/monitoring.conf

# Poll devices: one exec input for the whole node, common to every machine.
install -m 0644 "$REPO/deploy/telegraf.d/monitoring.conf" \
    /etc/telegraf/telegraf.d/pi-monitoring.conf

# Resident devices: one execd stanza each, committed per node. A node with
# none is normal and the loop simply finds nothing.
# Per NODE, not per site. A site's nodes have different devices: installing
# the whole site's fragments on one node would start collectors for hardware
# attached to a different machine.
resident=0
if [[ -d "$REPO/sites/$SITE/telegraf.d" ]]; then
    for fragment in "$REPO/sites/$SITE/telegraf.d/$NODE"-*.conf; do
        [[ -e "$fragment" ]] || continue
        install -m 0644 "$fragment" \
            "/etc/telegraf/telegraf.d/pi-monitoring-$(basename "$fragment")"
        resident=$((resident + 1))
    done
fi
echo "    1 poll fragment and $resident resident fragment(s) installed"
echo "    (inputs only; the output on this machine is left exactly as it was)"

# The node file is the authority on what should be running. Saying so here
# turns a mismatch into something visible at install rather than into data
# that silently never arrives.
echo "==> what this node expects to collect"
runuser -u telegraf -- /opt/monitoring/bin/collect \
    --node /etc/monitoring/node.yaml --list | sed "s/^/    /"

echo "==> checking devices"
# telegraf's resident children hold the GPIO pins and serial ports. Probing
# while they run gives false failures and steals bytes from a live stream.
if systemctl is-active --quiet telegraf; then
    systemctl stop telegraf
    telegraf_was_running=1
fi
# A warning, never a refusal. A dead five dollar sensor must not block a
# software update at a site nobody can reach; it fails loudly at runtime.
# As telegraf, not as root. Root can read a repository under a 0700 home
# directory that telegraf cannot, so checking as root would pass and every
# collection afterwards would fail.
if ! runuser -u telegraf -- /opt/monitoring/bin/collect \
        --node /etc/monitoring/node.yaml --check; then
    echo "    some devices failed. Continuing: this is a warning, because a"
    echo "    dead sensor must not block an update at a site nobody can reach."
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
