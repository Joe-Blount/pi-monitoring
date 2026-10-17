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

# Telegraf is stopped for two reasons below, and must come back however this
# script ends. Without a trap it stays stopped after a hang that the owner
# interrupts and after a dropped connection delivers SIGHUP, both of which
# leave the node silent with no error anywhere, an hour from anyone.
#
# It does NOT cover the kernel killing this shell for memory. SIGKILL cannot
# be trapped by anything. That case is handled by not running the package
# work beside a live collector in the first place, which is what the helpers
# below are for.
TELEGRAF_WAS_RUNNING=0
TELEGRAF_STOPPED=0

stop_telegraf_once() {
    [[ "$TELEGRAF_STOPPED" == "1" ]] && return 0
    if systemctl is-active --quiet telegraf; then
        TELEGRAF_WAS_RUNNING=1
        systemctl stop telegraf || true
    fi
    TELEGRAF_STOPPED=1
}

restore_telegraf() {
    # Whether or not it was running when this started. The normal path
    # restarts it unconditionally, and an earlier run of an older version of
    # this script could have left it stopped, in which case refusing to
    # start it here keeps the node silent for the same reason twice.
    #
    # errexit is turned off for the whole function rather than trusted to be
    # guarded line by line. When the terminal this was started from goes
    # away, a write to stderr fails on its own account, with nothing wrong
    # with telegraf at all, and under set -e that failure would end the shell
    # right here, before systemctl start runs. A node left silent because a
    # diagnostic line could not be printed is the worst trade in this file,
    # so no write in this function is allowed to decide whether telegraf
    # comes back.
    set +e
    if [[ "$TELEGRAF_STOPPED" == "1" ]] \
       && ! systemctl is-active --quiet telegraf; then
        echo "==> putting telegraf back" >&2 || true
        systemctl start telegraf || true
    fi
    set -e
    return 0
}

# SIGPIPE is ignored for the life of this script, which is what lets the
# EXIT trap below run at all when the far end of the output has gone.
#
# A connection dropped without a pty leaves stdout a pipe whose reader has
# exited. The next progress line then raises SIGPIPE, whose default action
# kills the shell outright: no trap runs, and telegraf stays stopped on a
# machine nobody is standing next to. Ignored, the same write returns an
# error instead, errexit unwinds, and the EXIT trap puts telegraf back.
#
# Ignoring rather than trapping. A PIPE trap that exits fires again inside
# the EXIT trap, on its first write, and the shell leaves before the restart.
#
# An ignored signal is inherited across exec, so every child started from
# here runs with it ignored too. That is a property of the whole install
# rather than of this line alone, and it is harmless because nothing here
# relies on a child dying from the signal: python already ignores it, and
# the rest write only to this script's own output.
trap '' PIPE

trap 'rc=$?; restore_telegraf; exit $rc' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Memory the kernel thinks a new process could actually get. Free memory is
# not the number: most of it is page cache the kernel hands back on demand.
available_mb() {
    awk '/^MemAvailable:/ {printf "%d", $2 / 1024; found = 1}
         END {if (!found) print 0}' /proc/meminfo 2>/dev/null || echo 0
}

# Below this, pip is refused. Its resolver is far heavier than apt, and the
# one recorded lock-up on the 418 MB machine was package work overlapping a
# Bluetooth scan. Stopping telegraf first removes the overlap; this guards
# the rest.
PIP_MIN_AVAILABLE_MB=300

echo "==> installing from"
if git -C "$REPO" rev-parse --short HEAD >/dev/null 2>&1; then
    echo "    commit $(git -C "$REPO" rev-parse --short HEAD)"
    if [[ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]]; then
        echo "    WARNING: this checkout has uncommitted changes, so what is"
        echo "             about to run is not what is in the repository:" >&2
        git -C "$REPO" status --short 2>/dev/null | sed "s/^/             /" >&2
    fi
else
    echo "    not a git checkout"
fi

echo "==> packages"

# Every package install goes through one of these two, and both stop telegraf
# first. Routing them rather than trusting each call site is the point: apt
# running beside a Bluetooth scan is the one recorded way this script has
# locked up a machine, and a locked machine needs somebody to drive to it. A
# call added later cannot forget.
install_from_apt() {
    stop_telegraf_once
    apt-get install -y --no-install-recommends "$1" >/dev/null 2>&1
}

#: Returned when there is not enough memory to risk pip, as distinct from pip
#: running and failing.
PIP_REFUSED=3

install_from_pip() {
    stop_telegraf_once
    if [[ "$(available_mb)" -lt "$PIP_MIN_AVAILABLE_MB" ]]; then
        return "$PIP_REFUSED"
    fi
    pip3 install --quiet "$1" >/dev/null 2>&1
}

# python3-yaml is the only hard requirement, and it comes first because the
# steps after it read the node file. Drivers that need more pull their own
# packages in as they are written, so a sensors-only node stays small.
if python3 -c "import yaml" 2>/dev/null; then
    echo "    python3-yaml already present"
else
    # Deliberately not fatal: a site with no uplink today must still be able
    # to take a code update.
    install_from_apt python3-yaml \
        || echo "    WARNING: could not install python3-yaml. Nothing will run"
fi

# Bluetooth drivers need bleak. Only when this node declares an enabled
# Bluetooth device: a sensors-only machine should not carry a stack it never
# uses.
#
# The node file is loaded rather than searched, and each driver says for itself
# whether it needs a radio. Matching the text instead was wrong twice over: it
# skipped a Bluetooth driver not named after its transport, and it matched
# devices that were commented out.
bluetooth_needed() {
    ( cd "$REPO" && python3 - "$NODE_FILE" <<'PYEOF'
import sys
try:
    from monitoring import config, drivers
    node = config.load(sys.argv[1], drivers.names())
except Exception as exc:
    sys.stderr.write("%s\n" % exc)
    sys.exit(2)
sys.exit(0 if any(
    getattr(drivers.get(d.driver), "needs_bluetooth", False)
    for d in node.devices if d.enabled) else 1)
PYEOF
    )
}

# Three outcomes, not two. One status has to mean "not needed" and a
# different one "the check could not answer", or a broken node file skips
# bleak silently and shows up as "needs bleak" at the first read instead.
if bluetooth_needed; then
    NEED_BLUETOOTH=yes
elif [ $? -eq 1 ]; then
    NEED_BLUETOOTH=no
else
    NEED_BLUETOOTH=unknown
fi

case "$NEED_BLUETOOTH" in
yes)
    if python3 -c "import bleak" 2>/dev/null; then
        echo "    bleak already present"
    elif install_from_apt python3-bleak; then
        echo "    python3-bleak installed from apt"
    else
        # Not packaged before Debian 12, and one of these machines runs 11.
        # Both sites have an uplink, so pip is a real fallback, and Debian 11
        # has no externally-managed restriction to argue with.
        # Captured rather than tested directly. Under set -e a non-zero
        # return from a bare command ends the script, so the refusal this
        # reports could never be reported: the installer simply exited,
        # on exactly the low-memory machine the refusal exists for.
        pip_status=0
        install_from_pip bleak || pip_status=$?
        case "$pip_status" in
        0)
            echo "    bleak installed with pip, which this release needs"
            ;;
        "$PIP_REFUSED")
            echo "    WARNING: only $(available_mb) MB available, so pip was"
            echo "             not run. Its resolver is heavy and has locked"
            echo "             up a small machine before. Install"
            echo "             python3-bleak by hand when there is room."
            ;;
        *)
            echo "    WARNING: could not install bleak, so Bluetooth devices"
            echo "             will fail with 'needs bleak'. Try:"
            echo "             pip3 install bleak"
            ;;
        esac
    fi
    ;;
no)
    ;;
unknown)
    echo "    WARNING: could not read the node file to decide about Bluetooth."
    echo "             Skipping bleak. The reason is above. If a pack later"
    echo "             fails with 'needs bleak', run: pip3 install bleak"
    ;;
esac

echo "==> directories"
install -d -m 0755 /etc/monitoring
install -d -m 0755 /var/lib/monitoring
# State files are written by whatever runs the collectors. telegraf needs to be
# able to keep a rain total or a lockout counter across a restart.
chgrp telegraf /var/lib/monitoring 2>/dev/null || true
chmod g+w /var/lib/monitoring 2>/dev/null || true

echo "==> where this machine is"
# A solar shed window needs a latitude, a longitude and a timezone. They are
# not in the node file on purpose: node files are committed and the
# repository is public, so coordinates in one would publish where the owner
# lives. See monitoring/control/location.py.
#
# A template is written, never values. Guessing a location produces a
# schedule that is wrong in a way nothing downstream can detect, which shows
# up months later as cameras keeping hours nobody asked for. location.py
# refuses to run against a file it cannot read as a real place, so the
# latitude and longitude lines are commented out of what lands here, even
# though the committed example carries numbers so its own comments still
# make sense read on their own. Installing it live would hand every machine
# that is never edited the same invented point, silently, which is exactly
# what the refusal in location.py exists to prevent.
#
# An existing file is left exactly as it is, because this script is run
# again on every update and overwriting it would undo the one thing a person
# had to type. But "left alone" has to mean something different for a file
# nobody has touched yet than for one somebody has actually filled in, or an
# operator reading install output can never tell an installed-but-unedited
# template from a real location -- so an unedited template is called out by
# name instead of folded into the same "left alone" line.
#
# Whether an existing file is usable is answered by the loader that will
# actually read it, not by searching the text for a marker. The same choice
# the Bluetooth probe above makes, for the same reason: a file where somebody
# uncommented one of the two lines matches any marker test you can write and
# is still refused at start, and the installer would have called it ready.
location_refusal() {
    ( cd "$REPO" && python3 - <<'PYEOF'
import sys
try:
    from monitoring.control import location
    location.read("/etc/monitoring/location.yaml")
except Exception as exc:
    sys.stderr.write("%s\n" % exc)
    sys.exit(1)
sys.exit(0)
PYEOF
    ) 2>&1
}

if [[ -e /etc/monitoring/location.yaml ]]; then
    if refusal="$(location_refusal)"; then
        echo "    /etc/monitoring/location.yaml exists, left alone"
    else
        echo "    /etc/monitoring/location.yaml will not load yet:"
        echo "    $refusal"
        echo "    Load control cannot start until that is fixed."
    fi
elif [[ -f "$REPO/location.yaml.example" ]]; then
    location_tmp="$(mktemp)"
    sed -E 's/^(latitude|longitude):/# \1:/' "$REPO/location.yaml.example" \
        > "$location_tmp"
    install -m 0644 "$location_tmp" /etc/monitoring/location.yaml
    rm -f "$location_tmp"
    echo "    wrote a TEMPLATE to /etc/monitoring/location.yaml"
    echo "    Edit it before enabling load control: uncomment latitude and"
    echo "    longitude and put in this machine's own numbers. Until then"
    echo "    location.read() refuses to run rather than use the commented-"
    echo "    out example point."
fi

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

# On the command search path, because that is how the field guide tells
# somebody to type them: `sudo control shedding off`, not a path they have to
# remember while standing at a machine with a dead battery. /opt/monitoring
# is not on anybody's PATH, and it is not on the one sudo substitutes either.
#
# /usr/local/bin rather than /usr/bin: it is for locally installed software,
# it is in the secure_path this distribution gives sudo, and nothing a
# package upgrade does will touch it.
#
# Links rather than copies, pointing through /opt/monitoring rather than at
# $REPO, so that a later install from a different directory needs only the
# one link above repointed and these keep working.
# A name already taken by something else is left alone and reported, the
# same way /opt/monitoring itself is above. Nothing in this distribution
# ships either name, so this is unlikely rather than impossible, and the
# cost of being wrong is taking somebody else's command without saying so.
# A link this installer already made is replaced without comment, which is
# what makes a reinstall from a different directory work.
for tool in collect control; do
    target="/usr/local/bin/$tool"
    if [[ -e "$target" || -L "$target" ]] \
       && [[ "$(readlink "$target" 2>/dev/null)" != "/opt/monitoring/bin/$tool" ]]; then
        echo "    WARNING: $target already exists and is not this project's" >&2
        echo "    link, so it is left alone. The field guide's bare" >&2
        echo "    '$tool' commands will run whatever that is." >&2
        continue
    fi
    ln -sfn "/opt/monitoring/bin/$tool" "$target"
done
echo "    collect and control on the path in /usr/local/bin"

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

echo "==> service environment"
# Two settings telegraf needs before it can drive a GPIO, both found the hard
# way on a real machine.
#
# A writable home. The telegraf user's home is /etc/telegraf and is not
# writable, so the gpiozero lgpio backend cannot create its notification
# files, silently falls back to the sysfs backend, and carries on.
#
# That fallback matters more than it sounds. The sysfs backend does not
# release a pin when the process dies, so every restart finds the pin busy and
# the collector never recovers. The character device backend does release it,
# which is also the property the whole load-shedding fail-safe rests on, so it
# is pinned here rather than left to a fallback nobody would notice.
install -d -m 0755 /etc/systemd/system/telegraf.service.d
cat > /etc/systemd/system/telegraf.service.d/pi-monitoring.conf <<'UNIT'
# Written by pi-monitoring's install script.
[Service]
Environment=HOME=/var/lib/monitoring
WorkingDirectory=/var/lib/monitoring
Environment=GPIOZERO_PIN_FACTORY=lgpio
UNIT
systemctl daemon-reload
echo "    telegraf given a writable home and the lgpio pin backend"

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
# Every fragment spelling this project has ever written, removed so that an
# installation carrying an older one is tidied. Both spellings matter: a
# monitoring-<node>-*.conf left behind beside its pi-monitoring counterpart
# starts two execd children for one device. They then fight over the serial
# port and the GPIO and restart every ten seconds for ever.
rm -f /etc/telegraf/telegraf.d/monitoring.conf
rm -f /etc/telegraf/telegraf.d/monitoring-*.conf

# Poll devices: one exec input for the whole node, common to every machine.
install -m 0644 "$REPO/deploy/telegraf.d/monitoring.conf" \
    /etc/telegraf/telegraf.d/pi-monitoring.conf

# A second collector for devices whose single read can take tens of seconds,
# installed only when this node declares one. Sharing the fast input would let
# one unreachable battery silence the whole node every cycle.
has_slow_devices() {
    ( cd "$REPO" && python3 - "$NODE_FILE" <<'PYEOF'
import sys
try:
    from monitoring import config, drivers, runner
    node = config.load(sys.argv[1], drivers.names())
except Exception as exc:
    sys.stderr.write("%s\n" % exc)
    sys.exit(2)
sys.exit(0 if runner.poll_devices(node, "slow") else 1)
PYEOF
    )
}

if has_slow_devices; then
    install -m 0644 "$REPO/deploy/telegraf.d/monitoring-slow.conf" \
        /etc/telegraf/telegraf.d/pi-monitoring-slow.conf
    echo "    installed the slow-device collector"
elif [ $? -ne 1 ]; then
    echo "    WARNING: could not tell whether this node has slow devices."
    echo "             Not installing the slow collector. If a Bluetooth"
    echo "             device never reports, that is why."
fi

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
stop_telegraf_once
# A warning, never a refusal. A dead five dollar sensor must not block a
# software update at a site nobody can reach; it fails loudly at runtime.
# As telegraf, not as root. Root can read a repository under a 0700 home
# directory that telegraf cannot, so checking as root would pass and every
# collection afterwards would fail.
# Bounded, because this runs with telegraf stopped. Most drivers here have no
# deadline of their own, and a device that accepts a connection and then stops
# answering would otherwise hold the whole node silent until somebody noticed.
CHECK_TIMEOUT=240
# Captured, not read after a negation. After `if ! cmd`, $? holds the result
# of the negation and is always 0, so the branch below that explains a
# timeout could never be reached and a killed check reported itself as a
# dead sensor. --foreground so the timeout stays in this process group and
# an operator's Ctrl-C reaches what is actually stuck.
check_status=0
timeout --foreground "$CHECK_TIMEOUT" runuser -u telegraf -- \
    /opt/monitoring/bin/collect --node /etc/monitoring/node.yaml --check \
    || check_status=$?
if [[ "$check_status" != "0" ]]; then
    status="$check_status"
    if [[ "$status" == "124" ]]; then
        echo "    the device check did not finish within ${CHECK_TIMEOUT}s and was" >&2
        echo "    stopped. Continuing, so telegraf comes back either way." >&2
    else
        echo "    some devices failed. Continuing: this is a warning, because a"
        echo "    dead sensor must not block an update at a site nobody can reach."
    fi
fi

echo "==> load controller"
# Installed on every node, started only where the node file asks for it. The
# files cost nothing idle, and having them already in place is what makes
# enabling control later a one line edit rather than another visit.
#
# The unit is installed before the decision about starting it, so that a node
# which turns control on tomorrow needs no deploy of its own.
if [[ -f "$REPO/deploy/monitoring-control.service" ]]; then
    install -m 0644 "$REPO/deploy/monitoring-control.service" \
        /etc/systemd/system/monitoring-control.service
    install -d -m 0755 /etc/tmpfiles.d
    install -m 0644 "$REPO/deploy/tmpfiles-monitoring.conf" \
        /etc/tmpfiles.d/monitoring.conf
    # Applied now rather than at the next boot. The unit's writable paths
    # must exist before it starts, and this script may be the thing starting
    # it a few lines below.
    #
    # Not fatal, for the same reason nothing else here is: a software update
    # must never be blocked on a machine nobody can reach. But it is reported,
    # because every collector on this node writes the reading cache under that
    # path, and a path telegraf cannot create turns into one complaint per
    # poll process with load control deciding from nothing. Silently carrying
    # on would leave somebody reading those lines later with no idea the
    # install had already seen the cause.
    if ! systemd-tmpfiles --create /etc/tmpfiles.d/monitoring.conf; then
        echo "    WARNING: could not create /run/monitoring. The collectors"
        echo "    will log that they cannot write the reading cache, and load"
        echo "    control cannot start. Readings still reach the database."
        echo "    Try: sudo systemd-tmpfiles --create" \
             "/etc/tmpfiles.d/monitoring.conf"
    fi
    systemctl daemon-reload

    # Whether this node runs control at all comes from the node file, read
    # with the same loader the controller uses rather than by grepping for a
    # line: a commented-out block must not count, and neither must the word
    # appearing in a comment.
    # Read with the loader the controller itself uses, the same way the
    # Bluetooth check above does and for the same reason: matching the text
    # counts a commented-out block, and the word appears in comments.
    CONTROL_WANTED=$(
        cd "$REPO" && python3 - "$NODE_FILE" <<'WANTS' 2>/dev/null
import sys
try:
    from monitoring import config, drivers
    node = config.load(sys.argv[1], drivers.names())
    print("yes" if node.control.get("enabled") else "no")
except Exception:
    print("unknown")
WANTS
    )
    CONTROL_WANTED="${CONTROL_WANTED:-unknown}"

    case "$CONTROL_WANTED" in
    yes)
        systemctl enable monitoring-control >/dev/null 2>&1 || true
        # Guarded, because errexit makes a bare failing command end the
        # script. A restart that fails would otherwise exit here, before the
        # check below that exists to report it as non-fatal, so the one
        # outcome this whole block is written to handle could never be
        # reached.
        systemctl restart monitoring-control || true
        sleep 2
        if systemctl is-active --quiet monitoring-control; then
            echo "    monitoring-control is running"
        else
            # Not fatal. A controller that will not start must not block a
            # software update on a machine nobody can reach, and every load
            # is sitting at its wired fail-safe while it is down.
            echo "    monitoring-control did NOT start. Loads are at their" >&2
            echo "    fail-safe. Check: journalctl -u monitoring-control -n 30" >&2
        fi
        ;;
    no)
        # Stopped as well as left disabled. A node whose control block was
        # switched off while the service was running would otherwise keep
        # driving relays from the block it started with.
        systemctl disable monitoring-control >/dev/null 2>&1 || true
        systemctl stop monitoring-control >/dev/null 2>&1 || true
        echo "    installed, not started: this node file has control disabled"
        ;;
    *)
        echo "    installed, not started: could not read the node file to" >&2
        echo "    tell whether this node runs control" >&2
        ;;
    esac
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
