import pathlib

import pytest

from monitoring import config, drivers

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Ask the registry rather than hard-coding a list. A fixed list goes stale
#: the day a driver is added, and then the committed node files fail for the
#: wrong reason.
KNOWN = drivers.names()


def write(tmp_path, text):
    path = tmp_path / "node.yaml"
    path.write_text(text)
    return path


MINIMAL = """
site: s
node: n
measurement: m
devices:
  - {name: host, driver: host, mode: poll}
"""


def test_a_minimal_node_loads(tmp_path):
    node = config.load(write(tmp_path, MINIMAL), KNOWN)
    assert node.site == "s" and node.node == "n" and node.measurement == "m"
    assert [d.name for d in node.devices] == ["host"]
    assert node.devices[0].enabled is True


def test_poll_devices_get_a_default_interval(tmp_path):
    node = config.load(write(tmp_path, MINIMAL), KNOWN)
    assert node.devices[0].interval == 30.0


def test_node_tags_are_merged_into_every_device(tmp_path):
    node = config.load(write(tmp_path, """
site: s
node: upstairs
measurement: m
tags: {node: upstairs}
devices:
  - {name: host, driver: host, mode: poll, tags: {location: pi}}
"""), KNOWN)
    assert node.devices[0].tags == {"node": "upstairs", "location": "pi",
                                    "device": "host"}


def test_every_device_is_tagged_with_its_own_name(tmp_path):
    """Two devices sharing a location would otherwise write into one series,
    and any field name they have in common silently overwrites. That became
    live the moment every reading started carrying a health field."""
    node = config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: box, driver: host, mode: poll, tags: {location: pi}}
  - {name: host, driver: host, mode: poll, tags: {location: pi}}
"""), KNOWN)
    assert node.devices[0].tags["device"] == "box"
    assert node.devices[1].tags["device"] == "host"
    assert node.devices[0].tags != node.devices[1].tags


def test_a_node_file_may_name_a_series_itself(tmp_path):
    """Overridable, for a file that wants two devices to share a series on
    purpose, or to keep a name that a dashboard already uses."""
    node = config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: internal_name, driver: host, mode: poll, tags: {device: public_name}}
"""), KNOWN)
    assert node.devices[0].tags["device"] == "public_name"


def test_a_device_tag_overrides_a_node_tag(tmp_path):
    node = config.load(write(tmp_path, """
site: s
node: n
measurement: m
tags: {location: default}
devices:
  - {name: host, driver: host, mode: poll, tags: {location: specific}}
"""), KNOWN)
    assert node.devices[0].tags["location"] == "specific"


@pytest.mark.parametrize("key", ["site", "node", "measurement"])
def test_a_missing_required_key_is_refused(tmp_path, key):
    text = "\n".join(line for line in MINIMAL.strip().splitlines()
                     if not line.startswith(key + ":"))
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, text), KNOWN)
    assert key in str(exc.value)


def test_an_unknown_driver_is_refused_with_the_known_list(tmp_path):
    """A misspelled driver must fail at load, not become a silent absence."""
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: x, driver: hsot, mode: poll}
"""), KNOWN)
    assert "hsot" in str(exc.value) and "host" in str(exc.value)


def test_an_unknown_mode_is_refused(tmp_path):
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: x, driver: host, mode: sometimes}
"""), KNOWN)
    assert "sometimes" in str(exc.value)


def test_duplicate_device_names_are_refused(tmp_path):
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: x, driver: host, mode: poll}
  - {name: x, driver: host, mode: poll}
"""), KNOWN)
    assert "two devices are named" in str(exc.value)


def test_two_devices_claiming_one_pin_are_refused(tmp_path):
    """Cheap to check, expensive to debug: usually one device silently reads
    nonsense while the other appears fine."""
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: a, driver: dht, mode: poll, params: {pin: 4}}
  - {name: b, driver: rain_gauge, mode: resident, params: {pin: 4}}
"""), KNOWN)
    assert "GPIO4" in str(exc.value)


def test_a_load_and_a_device_cannot_share_a_pin(tmp_path):
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: a, driver: dht, mode: poll, params: {pin: 23}}
control:
  enabled: true
  loads:
    cameras: {control_pin: 23}
"""), KNOWN)
    assert "GPIO23" in str(exc.value)


def test_an_unparsable_interval_is_refused(tmp_path):
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: x, driver: host, mode: poll, interval: soon}
"""), KNOWN)
    assert "soon" in str(exc.value)


def test_enabled_false_is_kept_not_dropped(tmp_path):
    """A disabled device stays declared, so its absence can be published."""
    node = config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: x, driver: host, mode: poll, enabled: false}
"""), KNOWN)
    assert len(node.devices) == 1 and node.devices[0].enabled is False


def test_control_enabled_without_loads_is_refused(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
control: {enabled: true}
"""), KNOWN)


def test_bad_yaml_names_the_file(tmp_path):
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, "site: s\n  node: bad indent\n"), KNOWN)
    assert "node.yaml" in str(exc.value)


def test_a_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(config.ConfigError) as exc:
        config.load(tmp_path / "nope.yaml", KNOWN)
    assert "cannot read" in str(exc.value)


@pytest.mark.parametrize("path", sorted(str(p) for p in (REPO / "sites").glob("*/*.yaml")))
def test_the_committed_site_files_are_valid(path):
    """The files actually deployed must pass the same validation as any other."""
    node = config.load(path, KNOWN)
    assert node.measurement and node.site and node.node


@pytest.mark.parametrize("value", [0, 1, "false", "no", None, []])
def test_enabled_must_be_a_real_boolean(tmp_path, value):
    """`enabled: 0` and `enabled: "false"` both read as true to a loose test,
    which is the wrong way round for a setting whose purpose is to stop
    something."""
    import yaml as _yaml
    text = ("site: s\nnode: n\nmeasurement: m\ndevices:\n"
            "  - {name: x, driver: host, mode: poll, enabled: %s}\n"
            % _yaml.safe_dump(value).strip())
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, text), KNOWN)
    assert "true or false" in str(exc.value)


@pytest.mark.parametrize("value", [True, False])
def test_a_real_boolean_is_accepted(tmp_path, value):
    node = config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: x, driver: host, mode: poll, enabled: %s}
""" % ("true" if value else "false")), KNOWN)
    assert node.devices[0].enabled is value


# -- refusals that existed with no test to show them ------------------------

def test_a_rain_gauge_in_poll_mode_is_refused(tmp_path):
    """Nothing watches the pin between polls, so the total never moves and the
    dashboard shows a working gauge in a drought. Deleting the check in the
    loader passed the whole suite before this existed."""
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: rain, driver: rain_gauge, mode: poll, params: {pin: 6}}
"""), KNOWN)
    assert "cannot work in" in str(exc.value)
    assert "resident" in str(exc.value)


def test_a_load_readback_pin_cannot_repeat_its_control_pin(tmp_path):
    """Two claims on one line, two levels down in the file, where the earlier
    duplicate of this check did not look."""
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
control:
  enabled: true
  loads:
    cameras:
      control_pin: 23
      readback: {source: pin, pin: 23}
"""), KNOWN)
    assert "GPIO23" in str(exc.value)


def test_a_guard_pin_cannot_repeat_a_pump_pin(tmp_path):
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
control:
  enabled: true
  loads:
    irrigation:
      pump: {control_pin: 22}
      guard: {input: pin, pin: 22}
"""), KNOWN)
    assert "GPIO22" in str(exc.value)


def test_a_pin_written_as_a_string_is_refused(tmp_path):
    """YAML will happily give a string where a pin number was meant, and the
    comparison against another device's integer pin then never matches."""
    with pytest.raises(config.ConfigError) as exc:
        config.load(write(tmp_path, """
site: s
node: n
measurement: m
devices:
  - {name: a, driver: dht, mode: poll, params: {pin: "4"}}
"""), KNOWN)
    assert "not a whole number" in str(exc.value)
