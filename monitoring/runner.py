"""Turn a node file into lines of InfluxDB line protocol.

Everything here is about behaving the same way for every driver, especially
when something fails. A device that cannot be read must produce a message on
standard error and must not take the other devices down with it: a dead five
dollar sensor should never stop a site reporting.

The one rule that shapes the rest: every declared device produces output every
interval. A disabled device publishes `enabled=0.0` and nothing else.
Therefore silence always means failure, never intent.
"""

import sys
import time

from . import calibration, lineproto
from . import drivers as driver_registry
from .drivers.base import DriverError


class Result:
    """What came of asking one device for a reading."""

    def __init__(self, device, lines=None, error=None, skipped=False):
        self.device = device
        self.lines = lines or []
        self.error = error
        self.skipped = skipped

    @property
    def ok(self):
        return self.error is None


def _now_ns():
    return int(time.time() * 1e9)


def build_driver(device):
    """Instantiate the driver a device declares."""
    cls = driver_registry.get(device.driver)
    return cls(device.name, device.params, device.tags)


#: Field names the runner adds to every point. No driver may use one: the
#: same name carrying a float from one device and a string from another is a
#: type conflict, and InfluxDB rejects the second. A test enforces this.
RESERVED_FIELDS = ("ok", "error_message", "enabled")

#: Longest failure reason written to standard error. telegraf truncates a
#: command's stderr at 512 bytes in total, so several long messages would push
#: each other out. The full reason goes into the published point instead,
#: where there is no such limit.
STDERR_REASON = 140


def point(node, device, fields, now_ns):
    """Render one reading as line protocol, with calibration applied.

    Every reading carries ok=1.0 so that a dashboard can ask whether a device
    is working without inferring it from which fields happen to be present.
    """
    corrected = calibration.apply(fields, device.calibration)
    corrected = dict(corrected)
    corrected.setdefault("ok", 1.0)
    return lineproto.line(node.measurement, device.tags, corrected, now_ns)


def failure_point(node, device, reason, now_ns):
    """What a device publishes when it could not be read.

    A failure that exists only in a log file on a machine at the end of a
    track is very nearly as silent as no failure at all. Publishing it puts
    the fault on the same dashboard as the data, and the reason is a field
    rather than a tag, so a changing string costs nothing.
    """
    return lineproto.line(
        node.measurement, device.tags,
        {"ok": 0.0, "error_message": str(reason)}, now_ns)


def disabled_point(node, device, now_ns):
    """What a deliberately disabled device publishes.

    It publishes something rather than nothing, so that an absence of data is
    always a failure and never a decision somebody made months ago.
    """
    return lineproto.line(node.measurement, device.tags, {"enabled": 0.0}, now_ns)


def read_device(node, device, now_ns=None):
    """Read one device. Never raises for a device fault."""
    now_ns = _now_ns() if now_ns is None else now_ns

    if not device.enabled:
        return Result(device, lines=[disabled_point(node, device, now_ns)], skipped=True)

    driver = None
    try:
        driver = build_driver(device)
        fields = driver.read()
        if not any(value is not None for value in (fields or {}).values()):
            # Every reading carries ok=1.0, so without this check a driver
            # that returned nothing would publish a point saying the device is
            # healthy and carrying no measurement at all. A read that produced
            # no value is a failed read.
            raise DriverError("the device returned no usable values")
        return Result(device, lines=[point(node, device, fields, now_ns)])
    except DriverError as exc:
        return Result(device, error=str(exc))
    except Exception as exc:  # a driver bug must not look like a dead sensor
        return Result(device, error="%s: %s" % (type(exc).__name__, exc))
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def poll_devices(node, speed="all"):
    """The poll devices a collector should read.

    Slow devices are separated because they share a process and a timeout with
    the fast ones. A Bluetooth pack that cannot be reached holds the process
    until telegraf kills it, and the kill discards every reading taken before
    it.

    "all" keeps the old behaviour for a node with nothing slow on it.
    """
    from . import drivers as _registry

    def is_slow(device):
        try:
            return bool(_registry.get(device.driver).slow_read)
        except Exception:
            return False

    devices = node.by_mode("poll")
    if speed == "fast":
        return [d for d in devices if not is_slow(d)]
    if speed == "slow":
        return [d for d in devices if is_slow(d)]
    return devices


def poll(node, now_ns=None, out=None, err=None, speed="all"):
    """Read poll-mode devices in this one process.

    One process per interval rather than one per device. Several interpreters
    starting on the same tick is enough load on a single-board computer to
    disturb a bit-banged sensor that is being read at the same moment, and the
    sensor is always the one that loses.

    Returns the number of lines written, which is what decides the exit
    code: anything written is worth keeping, including a point that reports a
    failure.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    now_ns = _now_ns() if now_ns is None else now_ns

    written = 0
    for device in poll_devices(node, speed):
        result = read_device(node, device, now_ns)
        if result.ok:
            for one in result.lines:
                out.write(one + "\n")
                written += 1
        else:
            # Both: the point so it reaches a dashboard, the log line so it
            # reaches whoever is reading journalctl at the time.
            out.write(failure_point(node, device, result.error, now_ns) + "\n")
            written += 1
            err.write("device %s (%s): %s\n"
                      % (device.name, device.driver,
                         str(result.error)[:STDERR_REASON]))

        # Flushed per device, not once at the end. Standard output to a pipe
        # is block buffered, so a process killed on timeout while reading a
        # later device would otherwise lose every reading already taken,
        # including the points reporting the failures.
        out.flush()
        err.flush()

    out.flush()
    err.flush()
    return written


def stream_device(node, device, out=None, err=None, max_readings=None,
                  sleep=time.sleep, clock=time.time):
    """Run a resident driver, printing each reading as it arrives.

    This is what telegraf's execd plugin runs: one long-lived process per
    device, which is the right shape for anything that talks when it feels
    like it rather than when it is asked.

    Output is flushed per line. A buffered stream would hold readings until
    the buffer filled, which on a sensor reporting every thirty seconds means
    data arriving in clumps hours late.

    A failure ends the process rather than being retried here. telegraf
    restarts an execd child after its restart delay, so the retry logic
    already exists and does not need writing twice. Returns an exit code.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    if not device.enabled:
        # A disabled resident device still reports, on the same cadence it
        # would have used, so that an absence of data never means "somebody
        # turned this off months ago".
        interval = device.interval or 30.0
        emitted = 0
        while max_readings is None or emitted < max_readings:
            out.write(disabled_point(node, device, int(clock() * 1e9)) + "\n")
            out.flush()
            emitted += 1
            if max_readings is not None and emitted >= max_readings:
                break
            sleep(interval)
        return 0

    driver = None
    try:
        driver = build_driver(device)
        emitted = 0
        for fields in driver.stream():
            out.write(point(node, device, fields, int(clock() * 1e9)) + "\n")
            out.flush()
            emitted += 1
            if max_readings is not None and emitted >= max_readings:
                return 0
        err.write("device %s (%s): the device stopped sending\n"
                  % (device.name, device.driver))
        return 1
    except DriverError as exc:
        err.write("device %s (%s): %s\n" % (device.name, device.driver, exc))
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        err.write("device %s (%s): %s: %s\n"
                  % (device.name, device.driver, type(exc).__name__, exc))
        return 1
    finally:
        err.flush()
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def orphaned(node):
    """Devices declared with no process that will ever read them.

    A device in controller mode is read by the controller, so if control is
    disabled on this node nothing collects it. That absence looks exactly like
    a sensor that is working and reporting nothing, which is the confusion
    this project keeps trying to remove.
    """
    if node.control.get("enabled"):
        return []
    return [d for d in node.by_mode("controller") if d.enabled]


def check(node, out=None):
    """Probe every declared device without taking a reading.

    The bring-up tool. It confirms an address answers, an id exists, a serial
    path resolves. A failure here is a warning, not a refusal: a dead sensor
    must never block a software update at a site nobody can reach.

    Returns the number of devices that failed.
    """
    out = sys.stdout if out is None else out
    failures = 0

    stranded = {d.name for d in orphaned(node)}

    for device in node.devices:
        if not device.enabled:
            out.write("  skip  %-16s disabled in configuration\n" % device.name)
            continue
        if device.name in stranded:
            failures += 1
            out.write("  FAIL  %-16s declared in controller mode, but control is "
                      "disabled on this node, so nothing will read it\n" % device.name)
            continue
        try:
            driver = build_driver(device)
        except DriverError as exc:
            failures += 1
            out.write("  FAIL  %-16s %s\n" % (device.name, exc))
            continue
        try:
            detail = driver.check()
            out.write("  ok    %-16s %s\n" % (device.name, detail))
        except DriverError as exc:
            failures += 1
            out.write("  FAIL  %-16s %s\n" % (device.name, exc))
        except Exception as exc:
            failures += 1
            out.write("  FAIL  %-16s %s: %s\n" % (device.name, type(exc).__name__, exc))
        finally:
            try:
                driver.close()
            except Exception:
                pass

    out.flush()
    return failures


def listing(node, out=None):
    """Print what this node declares, which is often enough to spot a typo."""
    out = sys.stdout if out is None else out
    out.write("%s / %s  ->  measurement %s\n"
              % (node.site, node.node, node.measurement))
    if node.tags:
        out.write("  tags: %s\n"
                  % ", ".join("%s=%s" % kv for kv in sorted(node.tags.items())))
    stranded = {d.name for d in orphaned(node)}
    for device in node.devices:
        state = "" if device.enabled else "  [disabled]"
        if device.name in stranded:
            state = "  [NOT COLLECTED: control is disabled]"
        # Shown as informational: telegraf schedules the single poll command,
        # so a per-device interval on a poll device describes an intention
        # rather than what happens.
        if device.interval is None:
            interval = ""
        elif device.mode == "poll":
            interval = "  (telegraf sets the rate)"
        else:
            interval = "  every %gs" % device.interval
        out.write("  %-16s %-12s %-11s%s%s\n"
                  % (device.name, device.driver, device.mode, interval, state))
    if node.control:
        state = "enabled" if node.control.get("enabled") else "disabled"
        loads = ", ".join(sorted(node.control.get("loads") or {})) or "none"
        out.write("  control: %s, loads: %s\n" % (state, loads))
    out.flush()
