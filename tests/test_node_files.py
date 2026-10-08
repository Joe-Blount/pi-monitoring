"""Checks on the node files that are actually deployed.

These are not about code coverage. They are about the class of mistake that a
unit test never sees: a node file that loads cleanly, lists cleanly, and then
fails on the machine because a driver needs a parameter nobody supplied.

Catching that here means it fails on a laptop in milliseconds rather than at a
site that takes a drive to reach.
"""

import pathlib
import re

import pytest

from monitoring import config, drivers, lineproto, runner
from monitoring.drivers.base import Driver

REPO = pathlib.Path(__file__).resolve().parent.parent
NODE_FILES = sorted((REPO / "sites").glob("*/*.yaml"))
IDS = ["%s/%s" % (p.parent.name, p.name) for p in NODE_FILES]


def load(path):
    return config.load(path, drivers.names())


# -- the registry itself -----------------------------------------------------

@pytest.mark.parametrize("name", sorted(drivers.names()))
def test_every_registered_driver_can_be_imported(name):
    """A typo in the registry is otherwise found by the device never working."""
    cls = drivers.get(name)
    assert issubclass(cls, Driver)


@pytest.mark.parametrize("name", sorted(drivers.names()))
def test_every_driver_declares_a_mode_it_can_work_in(name):
    cls = drivers.get(name)
    assert cls.modes, name
    assert set(cls.modes) <= set(config.MODES), cls.modes


@pytest.mark.parametrize("name", sorted(drivers.names()))
def test_every_driver_describes_itself(name):
    """--list and --check print this while someone is standing at a machine."""
    assert drivers.get(name).description.strip()


# -- the deployed node files -------------------------------------------------

@pytest.mark.parametrize("path", NODE_FILES, ids=IDS)
def test_every_declared_device_can_actually_be_constructed(path):
    """The gap between "the file parses" and "the device works".

    A driver raises for a parameter it cannot do without -- a serial port, a
    one-wire id -- when it is built, not when it is read. Without this, a node
    file missing one of those passes every other check and fails at the site.
    """
    node = load(path)
    for device in node.devices:
        # Keep state files out of the real /var/lib. Constructing a device
        # otherwise reads, and could create, the path a deployed machine uses.
        if "state_file" in device.params:
            device.params["state_file"] = None
        try:
            driver = runner.build_driver(device)
        except Exception as exc:
            pytest.fail("%s: device %r (%s) cannot be built: %s"
                        % (path.name, device.name, device.driver, exc))
        else:
            driver.close()





@pytest.mark.parametrize("path", NODE_FILES, ids=IDS)
def test_no_two_devices_share_a_tag_set(path):
    """Two devices with identical tags write into one series. If they ever
    share a field name, one value silently overwrites the other, and nothing
    reports it."""
    seen = {}
    for device in node_devices(load(path)):
        key = tuple(sorted(device.tags.items()))
        assert key not in seen, (
            "%s: %r and %r carry identical tags %s. They would share a series."
            % (path.name, seen[key], device.name, dict(key)))
        seen[key] = device.name


def node_devices(node):
    return [d for d in node.devices if d.enabled]


@pytest.mark.parametrize("path", NODE_FILES, ids=IDS)
def test_measurement_and_site_names_are_safe_in_line_protocol(path):
    """A space or comma in a measurement name is legal but has to be escaped
    everywhere it appears, including in queries nobody thinks to escape."""
    node = load(path)
    assert re.match(r"^[A-Za-z0-9_]+$", node.measurement), node.measurement
    for device in node.devices:
        for key, value in device.tags.items():
            assert re.match(r"^[A-Za-z0-9_]+$", str(key)), key
            assert re.match(r"^[A-Za-z0-9_.:-]+$", str(value)), value


# -- what the drivers actually emit ------------------------------------------

#: A float as Python renders it, including the scientific notation it uses
#: for small and large magnitudes. A calibration scale of 0.00001 produces
#: 1e-05, which InfluxDB accepts and a digits-dot-digits pattern rejects, so
#: the one authoritative test would fail on correct output.
FIELD = re.compile(r'^[A-Za-z0-9_]+=(".*"|-?\d+\.\d+([eE][-+]?\d+)?|-?\d+[eE][-+]?\d+)$')


def split_unescaped(text, sep):
    """Split on `sep` where it is neither escaped nor inside a quoted string.

    A regex cannot do this: a field value may legitimately contain the
    separator, as an error message containing a space or a comma does.
    """
    parts, current, quoted, escaped = [], [], False, False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == '"':
            quoted = not quoted
            current.append(char)
        elif char == sep and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def assert_valid_line(line):
    """Parse a line back, rather than matching substrings of it.

    Substring assertions pass on output that no database would accept. This
    checks the shape: measurement and tags, then fields, then a timestamp,
    with every numeric field a float and every string field quoted.
    """
    parts = split_unescaped(line, " ")
    assert len(parts) == 3, "expected measurement, fields and timestamp: %r" % line
    head, fields_part, timestamp = parts

    assert head, line
    assert timestamp.isdigit(), "no timestamp: %r" % line
    assert fields_part, "no fields: %r" % line

    for tag in split_unescaped(head, ",")[1:]:
        assert "=" in tag, "malformed tag %r in %r" % (tag, line)

    for field in split_unescaped(fields_part, ","):
        assert FIELD.match(field), (
            "field %r is neither a float nor a quoted string, so InfluxDB "
            "would take it as an integer or reject it: %r" % (field, line))


def test_the_validator_rejects_what_it_should():
    """A validator that accepts everything proves nothing about the lines it
    passes, so it is checked against known-bad output first."""
    with pytest.raises(AssertionError):
        assert_valid_line("m count=5 1700000000")        # integer, not a float
    with pytest.raises(AssertionError):
        assert_valid_line("m value=1.0")                 # no timestamp
    with pytest.raises(AssertionError):
        assert_valid_line("m 1700000000")                # no fields
    assert_valid_line("m,a=b value=1.0 1700000000")      # and accepts a good one
    assert_valid_line("m tiny=1e-05 1700000000")         # which InfluxDB accepts
    assert_valid_line("m big=1.5e+16 1700000000")


def test_every_driver_emits_lines_a_database_would_accept():
    """Every driver that can produce a reading without hardware, checked
    against the shape rather than against a substring."""
    from monitoring.drivers.ds18b20 import Ds18b20Driver
    from monitoring.drivers.dht import DhtDriver
    from monitoring.drivers.host import HostDriver
    from monitoring.drivers import vedirect
    from tests import vedirect_fixtures as fx

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    cases = [
        ("host", HostDriver("host", {"sysfs_root": str(fixtures / "host"),
                                     "report_address": False}, {}).read()),
        ("ds18b20", Ds18b20Driver("t", {"bus_root": str(fixtures / "w1"),
                                        "device_id": "28-3ce1d44326bf"}, {}).read()),
        ("dht", DhtDriver("box", {"iio_root": str(fixtures / "iio"),
                                  "retries": 0}, {}).read()),
        ("vedirect", vedirect.normalize(dict(fx.SUNNY))),
        ("pi30", known_driver_fields()["pi30"]),
        ("rain_gauge", known_driver_fields()["rain_gauge"]),
    ]
    for name, values in cases:
        line = lineproto.line("site", {"location": "x", "node": "n"},
                              dict(values, ok=1.0), 1700000000000000000)
        assert_valid_line(line)


def test_a_tag_value_needing_escapes_survives_the_round_trip():
    """Nothing in the committed files needs this today, which is exactly why
    it is worth pinning before something does."""
    line = lineproto.line("m", {"place": "back shed, north"},
                          {"v": 1.0}, 1700000000000000000)
    assert_valid_line(line)
    # Not line.split(" "): the separator appears inside the value, which is
    # the whole point of escaping it.
    head = split_unescaped(line, " ")[0]
    assert "back\\ shed\\,\\ north" in head
    assert len(split_unescaped(head, ",")) == 2, "the escaped comma split a tag"


# -- field names the runner reserves -----------------------------------------

def known_driver_fields():
    """Field names the drivers can actually produce, from the fixtures."""
    from monitoring.drivers.ds18b20 import Ds18b20Driver
    from monitoring.drivers.dht import DhtDriver
    from monitoring.drivers.host import HostDriver
    from monitoring.drivers.rain_gauge import RainCounter
    from monitoring.drivers import pi30, vedirect
    from tests import vedirect_fixtures as fx

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    sunny = ("230.1 50.0 230.1 50.0 0800 0750 015 420 53.20 010 085 0045 "
             "02.7 103.5 53.10 00000 00010101 00 02 01230")
    return {
        "host": HostDriver("h", {"sysfs_root": str(fixtures / "host")}, {}).read(),
        "ds18b20": Ds18b20Driver("t", {"bus_root": str(fixtures / "w1"),
                                       "device_id": "28-3ce1d44326bf"}, {}).read(),
        "dht": DhtDriver("b", {"iio_root": str(fixtures / "iio"), "retries": 0},
                         {}).read(),
        "vedirect": vedirect.normalize(dict(fx.SUNNY)),
        "pi30": pi30.parse_qpigs(sunny),
        "rain_gauge": RainCounter(None, 0.011).fields(),
    }


@pytest.mark.parametrize("driver_name", sorted(known_driver_fields()))
def test_no_driver_uses_a_field_name_the_runner_reserves(driver_name):
    """The same name carrying a float from one device and a string from
    another is a type conflict, and InfluxDB rejects the second. The point it
    would reject is the one whose entire job is to report a failure."""
    from monitoring.runner import RESERVED_FIELDS
    clash = set(known_driver_fields()[driver_name]) & set(RESERVED_FIELDS)
    assert not clash, (
        "%s emits %s, which the runner also writes. One of them would be "
        "rejected." % (driver_name, ", ".join(sorted(clash))))


def test_the_runner_renders_its_reserved_fields_with_one_type_each(tmp_path):
    """Across a reading, a failure and a disabled device."""
    from monitoring import runner as mod
    node = config.load(NODE_FILES[0], drivers.names())
    device = node.devices[0]

    rendered = {}
    for line in (mod.point(node, device, {"v": 1.0}, 1),
                 mod.failure_point(node, device, "a reason", 1),
                 mod.disabled_point(node, device, 1)):
        for field in split_unescaped(split_unescaped(line, " ")[1], ","):
            name, _, value = field.partition("=")
            kind = "string" if value.startswith('"') else "number"
            if name in mod.RESERVED_FIELDS:
                assert rendered.setdefault(name, kind) == kind, (
                    "%s is written as both a %s and a %s"
                    % (name, rendered[name], kind))


@pytest.mark.parametrize("nasty", ["two\nlines", "carriage\rreturn",
                                   "windows\r\nstyle"])
def test_a_line_break_in_a_value_cannot_split_a_point(nasty):
    """A newline ends a point. One exception message containing one would
    leave an unterminated quote, and telegraf fails the whole batch on a
    malformed line, costing every device its reading for that interval."""
    line = lineproto.line("m", {"device": "d"},
                          {"ok": 0.0, "error_message": nasty}, 1700000000)
    assert len(line.splitlines()) == 1
    assert_valid_line(line)


def test_a_line_break_in_a_tag_value_cannot_split_a_point():
    line = lineproto.line("m", {"device": "two\nlines"}, {"v": 1.0}, 1700000000)
    assert len(line.splitlines()) == 1


@pytest.mark.parametrize("site", sorted({p.parent.name for p in NODE_FILES}))
def test_each_node_of_a_site_is_tagged_distinctly(site):
    """Two machines at one site write the same measurement. Only the node tag
    separates them, so a node file copied to make a third machine with that
    line forgotten puts two Pis into one series, overwriting each other with
    no error anywhere."""
    seen = {}
    for path in sorted((REPO / "sites" / site).glob("*.yaml")):
        node = load(path)
        tag = node.tags.get("node")
        assert tag, "%s sets no node tag" % path.name
        assert tag not in seen, (
            "%s and %s both tag themselves %r; they would share every series"
            % (seen[tag], path.name, tag))
        seen[tag] = path.name


def battery_heating_loads():
    """Every battery heating load declared by any node file."""
    found = []
    for path in NODE_FILES:
        node = load(path)
        for name, spec in (node.control.get("loads") or {}).items():
            if spec.get("type") == "battery_heating":
                found.append(("%s/%s" % (path.parent.name, name), spec))
    return found


def test_battery_heating_fails_off():
    """A dead Pi leaving cameras on costs a flat battery, which the BMS
    bounds. A dead Pi leaving a heater on against a battery has no bound."""
    for label, spec in battery_heating_loads():
        assert spec.get("polarity") == "energize_to_connect", label


def test_battery_heating_declares_what_to_do_without_a_temperature():
    """Either answer is defensible and they need different hardware, so the
    file must say which. Falling back reproduces a plain thermostatic heater,
    which is safe only because a mechanical cutout sits in series; refusing
    costs a winter of charging whenever the radio is unreliable."""
    for label, spec in battery_heating_loads():
        choice = spec.get("on_stale_data")
        assert choice in ("fallback", "deny"), "%s: %r" % (label, choice)
        if choice == "fallback":
            assert (spec.get("temperature") or {}).get("fallback_sources"), label


def test_battery_heating_has_a_hard_upper_limit():
    """The working thresholds aim at a target. This one is the stop that does
    not care what the target was."""
    for label, spec in battery_heating_loads():
        assert (spec.get("stop_if_any") or {}).get("any_sensor_above_f"), label


def test_battery_heating_thresholds_are_not_inverted():
    """Stop warmer than start, or the load either never runs or never stops.
    The gap is the deadband, and the thermal loop is slow enough to need a
    wide one."""
    for label, spec in battery_heating_loads():
        start = (spec.get("heat_if_all") or {})["coldest_below_f"]
        stop = (spec.get("stop_if_any") or {})["coldest_above_f"]
        assert stop > start, "%s: stops at %s, starts at %s" % (label, stop, start)
        assert stop - start >= 5.0, "%s: deadband is only %s F" % (label, stop - start)


def test_battery_heating_reads_more_than_one_temperature():
    """The coldest sensor governs, which needs more than one to mean anything.
    These packs report about two sensors each."""
    for label, spec in battery_heating_loads():
        sources = (spec.get("temperature") or {}).get("sources") or []
        assert len(sources) >= 2, "%s: %d source(s)" % (label, len(sources))


def test_battery_heating_waits_on_production_not_on_charge_current():
    """A pack too cold to charge accepts nothing, so charge current is zero
    exactly when heating is needed. Gating on it would make the condition
    unsatisfiable. Production is measured before the battery has a say."""
    for label, spec in battery_heating_loads():
        source = ((spec.get("require_production") or {}).get("source") or {})
        field = source.get("field", "")
        assert "amps" not in field and "current" not in field, "%s: %s" % (label, field)
        assert field, label
