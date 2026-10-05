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


def _check_pins(path, devices, control):
    """Refuse a file where two things claim one GPIO pin.

    This is cheap to check and expensive to debug: a duplicated pin usually
    shows up as one device working and another silently reading nonsense.
    """
    claimed = {}

    def claim(pin, owner):
        if pin is None:
            return
        if not isinstance(pin, int):
            _fail(path, "%s: pin %r is not a whole number" % (owner, pin))
        if pin in claimed:
            _fail(path, "GPIO%d is claimed by both %s and %s" % (pin, claimed[pin], owner))
        claimed[pin] = owner

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

        interval = entry.get("interval")
        if interval is not None:
            try:
                interval = duration.seconds(interval, "device %r interval" % name)
            except duration.DurationError as exc:
                _fail(path, str(exc))
        elif mode == "poll":
            interval = 30.0

        tags = dict(node.tags)
        tags.update(entry.get("tags") or {})

        node.devices.append(Device(
            name=name,
            driver=driver,
            mode=mode,
            tags=tags,
            params=dict(entry.get("params") or {}),
            calibration=dict(entry.get("calibration") or {}),
            enabled=entry.get("enabled", True) is not False,
            interval=interval,
        ))

    _check_pins(path, node.devices, node.control)

    if node.control.get("enabled") and not node.control.get("loads"):
        _fail(path, "control is enabled but no loads are defined")

    return node
