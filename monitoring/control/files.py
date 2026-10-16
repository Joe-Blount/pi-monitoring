"""The files load control reads and writes, and which medium each belongs on.

Three kinds of state, and the difference matters more than it looks.

**Readings**, in ``/run``. The collectors publish what they read and the
control loop consumes it, because these devices admit one reader at a time and
a controller opening the battery itself would fight the collector every pass.
Written about three thousand times a day, so it must not touch the SD card:
``/run`` is tmpfs on any systemd machine, while ``/tmp`` on this distribution
is a directory on the card and would wear it exactly the same.

One file per device rather than one file for all of them. Three collectors
write this directory, the fast poll, the slow poll and each resident child,
and a single shared file would be a read-modify-write race producing torn
JSON. Separate files written by temp-and-rename are atomic within a directory,
so a reader sees either the old reading or the new one and never half of each.

**Loop state**, in ``/run``. The latch set, the floor, the transition stamps.
Losing it on reboot costs nothing, because the loop takes no action before a
reading arrives anyway.

**Switches**, on the card. An override and the shedding enable have to survive
a reboot, or disabling shedding and then losing power would quietly re-enable
it. Written only when an operator types a command, so a handful of writes a
year.

Monotonic stamps and the boot identity
--------------------------------------
Ages and dwells are measured on the monotonic clock, because the wall clock on
these machines steps by hours the first time it syncs after a stale restore,
and every dwell would expire at once. But the monotonic clock restarts at zero
every boot, so a monotonic stamp read back from the card after a reboot is
meaningless: a stamp of four hundred thousand seconds compared against a
current reading of sixty would hold a relay for days.

So anything persisted alongside a monotonic stamp carries the boot identity,
and a reader discards monotonic stamps from a different boot. The wall stamp is
kept beside it for the things that genuinely span boots.
"""

import json
import os
import time

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

#: Where readings and loop state live. tmpfs, so writes never reach the card.
RUNTIME_DIR = "/run/monitoring"
READINGS_DIR = os.path.join(RUNTIME_DIR, "readings")

#: The loop's own working state: latches, floor, transition stamps. Private to
#: the loop and meaningless to anything else.
CONTROL_STATE = os.path.join(RUNTIME_DIR, "control-state.json")

#: What the loop says it did, for the publishing driver to turn into fields.
#: Separate from the working state so the driver reads a document written for
#: it rather than the loop's internals, and so a change to one does not have
#: to care about the other.
PUBLISHED = os.path.join(RUNTIME_DIR, "control.json")

#: Where the things that must outlive a reboot live. On the card,
#: deliberately, and written rarely enough that the wear does not matter.
PERSIST_DIR = "/var/lib/monitoring"
SWITCHES = os.path.join(PERSIST_DIR, "switches.json")
HIGH_WATER = os.path.join(PERSIST_DIR, "clock-high-water.json")


#: Every path below is resolved when the function is called, never bound as a
#: default argument. A default is evaluated once, when the module is imported,
#: so a caller or a test that redirects one of the constants above would be
#: silently ignored and would read and write the real paths instead. It is
#: written down because the failure is quiet in the worst way: the test still
#: passes, answered out of the real directory.
def boot_id():
    """An identifier for this boot, or None where the kernel does not say.

    None rather than a fabricated value, so a reader that cannot tell which
    boot a stamp came from treats it as foreign rather than assuming it is
    current.
    """
    try:
        with open(BOOT_ID_PATH) as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def monotonic():
    """Seconds on a clock that only moves forward and never steps.

    System-wide within a boot, so a stamp written by a collector is
    comparable with one read by the control loop.
    """
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def read_json(path):
    """Parse a JSON file, or None if it is missing, empty or torn.

    None for a damaged file rather than an exception, because the caller's
    correct response to both is the same: treat it as absent. A torn file is
    a real possibility even with atomic writes, since a machine can lose
    power between the write and the rename.
    """
    try:
        with open(path) as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path, data):
    """Write a JSON file so a reader never sees it half written.

    Temp file in the same directory, flushed, then renamed. Rename within a
    directory is atomic, so a reader opening the path gets the whole of one
    version or the whole of the other.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(temporary, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


# -- readings ---------------------------------------------------------------

def publish_reading(fields, device, directory=None, now=None,
                    boot=None):
    """Record what a collector just read, for the control loop to consume.

    Called from the collector rather than from the loop, which is the whole
    point: the device is opened once, by whoever already had the right to.

    The clock and the boot identity are arguments rather than read here, so a
    test can hold both still. A reading's age is only meaningful against the
    boot it was stamped in, and that is the thing most worth being able to
    lie about in a test.
    """
    directory = READINGS_DIR if directory is None else directory
    write_json(os.path.join(directory, "%s.json" % device), {
        "device": device,
        "fields": {name: value for name, value in fields.items()
                   if isinstance(value, (int, float))
                   and not isinstance(value, bool)},
        "boot_id": boot_id() if boot is None else boot,
        "monotonic": monotonic() if now is None else now,
    })


def read_readings(directory=None, now=None, this_boot=None):
    """Every device reading currently published, with its age in seconds.

    A reading from another boot, or one with no boot identity, comes back with
    an infinite age rather than being dropped. The caller then treats it as
    stale, which is honest: the values may be real but nothing here can say
    how old they are.

    Each reading also carries ``taken_at``, the monotonic moment the collector
    published it. The age is derived from it and moves on every call, so an
    age cannot tell a caller whether two reads saw the same sample; the stamp
    can, and a ladder that counts readings has to be able to. It is None
    whenever the age is infinite, because a stamp from a clock that has since
    restarted identifies nothing.
    """
    directory = READINGS_DIR if directory is None else directory
    now = monotonic() if now is None else now
    this_boot = boot_id() if this_boot is None else this_boot
    out = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out

    for name in names:
        if not name.endswith(".json"):
            continue
        record = read_json(os.path.join(directory, name))
        if not record or not isinstance(record.get("fields"), dict):
            continue
        device = record.get("device") or name[:-len(".json")]
        stamp = record.get("monotonic")
        same_boot = (this_boot is not None
                     and record.get("boot_id") == this_boot)
        if same_boot and isinstance(stamp, (int, float)):
            taken_at = float(stamp)
            age = max(0.0, now - taken_at)
        else:
            taken_at = None
            age = float("inf")
        out[device] = {"fields": record["fields"], "age_seconds": age,
                       "taken_at": taken_at}
    return out


# -- loop state -------------------------------------------------------------

def read_state(path=None, this_boot=None):
    """The loop's own state, with anything from another boot discarded.

    Returns an empty mapping rather than None, because a missing state file
    and a foreign one both mean the same thing to the caller: start from
    nothing, take no action until a reading arrives.
    """
    record = read_json(CONTROL_STATE if path is None else path)
    if not record:
        return {}
    this_boot = boot_id() if this_boot is None else this_boot
    if this_boot is None or record.get("boot_id") != this_boot:
        # Monotonic stamps inside are from a clock that has since restarted
        # at zero. Keeping them would hold a dwell for as long as the machine
        # had previously been up.
        return {}
    return record.get("loads") or {}


def write_state(loads, path=None, boot=None):
    write_json(CONTROL_STATE if path is None else path,
               {"boot_id": boot_id() if boot is None else boot,
                "loads": loads})


# -- the clock's high-water mark --------------------------------------------

def read_high_water(path=None):
    """The latest wall time this node has ever trusted, or None.

    On the card rather than in tmpfs, and this is the whole point of it. The
    rule that catches a reset clock is "the hardware must not read earlier
    than a time already seen", and a mark that died with the power cut would
    be None exactly when it is needed. A board whose clock resets would then
    answer, have nothing to be compared against, and be trusted on its first
    boot with a wrong date.
    """
    record = read_json(HIGH_WATER if path is None else path) or {}
    value = record.get("epoch")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


#: How far the wall clock must move before the mark is rewritten.
#:
#: This is the only file the loop writes to the card, and the wall clock
#: moves on every pass, so writing whenever it is later means writing every
#: pass: at a ten second interval, about nine thousand fsynced writes a day
#: on a card with no spare.
#:
#: Coarse granularity costs the check nothing. What the mark catches is a
#: board whose clock resets, and a reset clock does not read an hour early,
#: it reads 1970 or the year its chip powers up in. The margin being hours
#: rather than seconds does not bring that anywhere near the bound. What the
#: mark would not catch at any granularity is a restored stale time, which
#: comes back within seconds of the last shutdown; the synchronisation
#: marker is what covers that, and it is in tmpfs.
ADVANCE_STEP = 3600.0


def note_high_water(epoch, path=None):
    """Record a trusted wall time, if it is well past the one already held.

    Only ever advanced, and only from a clock that was trusted when it was
    read. Advancing it from an untrusted clock would let one fast wrong
    reading poison the mark permanently, after which no real clock could
    ever clear it.

    Returns the mark in force, which is the held value when the clock has
    not moved far enough to be worth a write.
    """
    path = HIGH_WATER if path is None else path
    held = read_high_water(path)
    if held is None:
        # Nothing on the card yet, so write at once. Until this exists the
        # RTC has nothing to be compared against and is trusted on sight.
        write_json(path, {"epoch": float(epoch)})
        return float(epoch)
    if epoch <= held + ADVANCE_STEP:
        return held
    write_json(path, {"epoch": float(epoch)})
    return float(epoch)


# -- switches ---------------------------------------------------------------

def read_switches(path=None):
    """The operator's overrides and the shedding enable.

    An unreadable or damaged file reads as no override and shedding enabled,
    which is the state the system is in when nobody has touched it. Failing
    closed here would let a corrupt file shed loads nobody asked to shed.
    """
    parsed = read_json(SWITCHES if path is None else path)
    record = parsed or {}
    loads = record.get("loads")
    return {
        "shedding_enabled": record.get("shedding_enabled", True) is not False,
        "loads": loads if isinstance(loads, dict) else {},
        "readable": parsed is not None,
    }


def write_switches(switches, path=None):
    write_json(SWITCHES if path is None else path, {
        "shedding_enabled": bool(switches.get("shedding_enabled", True)),
        "loads": switches.get("loads") or {},
    })
