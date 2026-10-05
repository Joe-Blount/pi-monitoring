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


def point(node, device, fields, now_ns):
    """Render one reading as line protocol, with calibration applied."""
    corrected = calibration.apply(fields, device.calibration)
    return lineproto.line(node.measurement, device.tags, corrected, now_ns)


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


def poll(node, now_ns=None, out=None, err=None):
    """Read every poll-mode device in this one process.

    One process per interval rather than one per device. Several interpreters
    starting on the same tick is enough load on a single-board computer to
    disturb a bit-banged sensor that is being read at the same moment, and the
    sensor is always the one that loses.

    Returns the number of devices that failed.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    now_ns = _now_ns() if now_ns is None else now_ns

    failures = 0
    for device in node.by_mode("poll"):
        result = read_device(node, device, now_ns)
        if result.ok:
            for one in result.lines:
                out.write(one + "\n")
        else:
            failures += 1
            err.write("device %s (%s): %s\n" % (device.name, device.driver, result.error))
    out.flush()
    err.flush()
    return failures


def check(node, out=None):
    """Probe every declared device without taking a reading.

    The bring-up tool. It confirms an address answers, an id exists, a serial
    path resolves. A failure here is a warning, not a refusal: a dead sensor
    must never block a software update at a site nobody can reach.

    Returns the number of devices that failed.
    """
    out = sys.stdout if out is None else out
    failures = 0

    for device in node.devices:
        if not device.enabled:
            out.write("  skip  %-16s disabled in configuration\n" % device.name)
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
    for device in node.devices:
        state = "" if device.enabled else "  [disabled]"
        interval = "" if device.interval is None else "  every %gs" % device.interval
        out.write("  %-16s %-12s %-11s%s%s\n"
                  % (device.name, device.driver, device.mode, interval, state))
    if node.control:
        state = "enabled" if node.control.get("enabled") else "disabled"
        loads = ", ".join(sorted(node.control.get("loads") or {})) or "none"
        out.write("  control: %s, loads: %s\n" % (state, loads))
    out.flush()
