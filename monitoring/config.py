"""Load and validate a node file.

Validation is strict and the messages name the file and the device, because
the alternative is discovering a typo as silence on a dashboard days later,
on a machine that takes an hour of driving to reach.

Everything that can be checked without hardware is checked here: unknown
drivers, unknown modes, duplicate names, two devices claiming one GPIO pin,
durations that cannot be parsed. What cannot be checked here, such as whether
a 1-Wire id actually exists, is left to `collect --check`, which probes.
"""

import pathlib

import yaml

from . import duration

MODES = ("poll", "resident", "controller")

#: Every kind of load a control block may declare.
#:
#: Validated because tests that look for loads of a given type filter on this
#: string. A typo there does not fail: it yields no loads, and every test about
#: that load passes by finding nothing to check. The load itself would also
#: never be driven.
LOAD_TYPES = ("dc_branch", "sequenced_ac", "battery_heating", "vent_fan")

REQUIRED_TOP_LEVEL = ("site", "node", "measurement")


class ConfigError(ValueError):
    """A node file that cannot be used as written."""


class Device:
    """One declared device on this machine."""

    def __init__(self, name, driver, mode, tags, params, calibration,
                 enabled=True, interval=None):
        self.name = name
        self.driver = driver
        self.mode = mode
        self.tags = tags
        self.params = params
        self.calibration = calibration
        self.enabled = enabled
        self.interval = interval

    def __repr__(self):
        return "<Device %s driver=%s mode=%s enabled=%s>" % (
            self.name, self.driver, self.mode, self.enabled)


class Node:
    """One machine: what it has, and what to do with it."""

    def __init__(self, path, raw):
        self.path = path
        self.raw = raw
        self.site = raw["site"]
        self.node = raw["node"]
        self.measurement = raw["measurement"]
        self.tags = dict(raw.get("tags") or {})
        self.battery = raw.get("battery") or {}
        self.control = raw.get("control") or {}
        self.devices = []

    def by_mode(self, mode):
        return [d for d in self.devices if d.mode == mode]

    def find(self, name):
        for device in self.devices:
            if device.name == name:
                return device
        return None

    def __repr__(self):
        return "<Node %s/%s devices=%d>" % (self.site, self.node, len(self.devices))


def _fail(path, message):
    raise ConfigError("%s: %s" % (path, message))


def _enabled(path, name, value):
    """Only a real boolean turns a device off.

    `enabled: 0` and `enabled: "false"` both read as true to a loose test,
    which is the wrong way round for a setting whose whole purpose is to stop
    something.
    """
    if not isinstance(value, bool):
        _fail(path, "device %r has enabled: %r; it must be true or false"
                    % (name, value))
    return value


def claimed_pins(devices, control, on_conflict=None, on_bad_pin=None):
    """Every GPIO pin something in a node file claims, and what claims it.

    One function rather than two, because the second copy lived in the GPIO
    probe and missed the pins nested under a load's inverter and pump. The
    probe would therefore have driven the inverter relay while reporting the
    pin as free.
    """
    claimed = {}

    def claim(pin, owner):
        if pin is None:
            return
        if not isinstance(pin, int) or isinstance(pin, bool):
            if on_bad_pin:
                on_bad_pin(pin, owner)
            return
        if pin in claimed and on_conflict:
            on_conflict(pin, claimed[pin], owner)
        claimed.setdefault(pin, owner)

    for device in devices:
        params = device.params
        for key in ("pin", "data_pin", "control_pin", "readback_pin"):
            claim(params.get(key), "device %s" % device.name)

    for load_name, load in (control.get("loads") or {}).items():
        claim(load.get("control_pin"), "load %s" % load_name)
        readback = load.get("readback") or {}
        if readback.get("source") == "pin":
            claim(readback.get("pin"), "load %s readback" % load_name)
        for part_name in ("inverter", "pump"):
            part = load.get(part_name)
            if not isinstance(part, dict):
                continue
            claim(part.get("control_pin"), "load %s %s" % (load_name, part_name))
            part_readback = part.get("readback") or {}
            if part_readback.get("source") == "pin":
                claim(part_readback.get("pin"),
                      "load %s %s readback" % (load_name, part_name))
        guard = load.get("guard") or {}
        if guard.get("input") == "pin":
            claim(guard.get("pin"), "load %s guard" % load_name)

    return claimed


def _check_pins(path, devices, control):
    """Refuse a file where two things claim one GPIO pin.

    Cheap to check and expensive to debug: a duplicated pin usually shows up
    as one device working and another silently reading nonsense.
    """
    claimed_pins(
        devices, control,
        on_conflict=lambda pin, first, second: _fail(
            path, "GPIO%d is claimed by both %s and %s" % (pin, first, second)),
        on_bad_pin=lambda pin, owner: _fail(
            path, "%s: pin %r is not a whole number" % (owner, pin)))


def load(path, known_drivers=None):
    """Read a node file and return a validated Node.

    `known_drivers` is the set of driver names the running code understands.
    Passing it turns a misspelled driver into an error at load rather than a
    device that silently never reports.
    """
    path = pathlib.Path(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError("%s is not valid YAML: %s" % (path, exc))

    if not isinstance(raw, dict):
        _fail(path, "the top level must be a mapping")

    for key in REQUIRED_TOP_LEVEL:
        if not raw.get(key):
            _fail(path, "missing required key %r" % key)

    node = Node(path, raw)

    devices_raw = raw.get("devices")
    if devices_raw is None:
        devices_raw = []
    if not isinstance(devices_raw, list):
        _fail(path, "'devices' must be a list")

    seen = set()
    for index, entry in enumerate(devices_raw):
        if not isinstance(entry, dict):
            _fail(path, "device %d is not a mapping" % index)

        name = entry.get("name")
        if not name:
            _fail(path, "device %d has no name" % index)
        if name in seen:
            _fail(path, "two devices are named %r" % name)
        seen.add(name)

        driver = entry.get("driver")
        if not driver:
            _fail(path, "device %r has no driver" % name)
        if known_drivers is not None and driver not in known_drivers:
            _fail(path, "device %r uses unknown driver %r; known drivers are %s"
                  % (name, driver, ", ".join(sorted(known_drivers))))

        mode = entry.get("mode")
        if mode not in MODES:
            _fail(path, "device %r has mode %r; it must be one of %s"
                  % (name, mode, ", ".join(MODES)))

        if known_drivers is not None:
            # A driver in a mode it cannot work in is worse than an error:
            # it validates, lists, checks, and then publishes nothing useful.
            from . import drivers as _registry
            try:
                allowed = _registry.get(driver).modes
            except Exception:
                allowed = None
            if allowed is not None and mode not in allowed:
                _fail(path, "device %r uses driver %r in %s mode, which it "
                            "cannot work in; it supports %s"
                            % (name, driver, mode, ", ".join(allowed)))

        interval = entry.get("interval")
        if interval is not None:
            try:
                interval = duration.seconds(interval, "device %r interval" % name)
            except duration.DurationError as exc:
                _fail(path, str(exc))
        elif mode == "poll":
            interval = 30.0

        # The device's own name, so that two devices sharing a location
        # cannot write into one series. Without it they collide on any field
        # name they have in common, and one value silently replaces the other
        # with no error anywhere. Overridable, for a node file that wants to
        # name a series something else.
        tags = dict(node.tags)
        tags["device"] = name
        tags.update(entry.get("tags") or {})

        node.devices.append(Device(
            name=name,
            driver=driver,
            mode=mode,
            tags=tags,
            params=dict(entry.get("params") or {}),
            calibration=dict(entry.get("calibration") or {}),
            enabled=_enabled(path, name, entry.get("enabled", True)),
            interval=interval,
        ))

    _check_pins(path, node.devices, node.control)

    for load_name, load in (node.control.get("loads") or {}).items():
        if not isinstance(load, dict):
            _fail(path, "load %r is not a mapping" % load_name)
        kind = load.get("type")
        if kind not in LOAD_TYPES:
            _fail(path, "load %r has type %r; it must be one of %s"
                        % (load_name, kind, ", ".join(LOAD_TYPES)))

    if node.control.get("enabled") and not node.control.get("loads"):
        _fail(path, "control is enabled but no loads are defined")

    return node
