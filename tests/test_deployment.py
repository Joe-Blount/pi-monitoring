"""The node file and the telegraf fragments must agree.

This is the gap that prompted these tests. The blind's node declares two
resident devices, the install script installed only the poll fragment, and
deploying it would have collected temperature while the solar and rain data
simply never appeared. Nothing would have reported an error.

A disagreement between the two must therefore fail here rather than present as
missing data on a machine an hour away.
"""

import pathlib
import re

import pytest

from monitoring import config, drivers

REPO = pathlib.Path(__file__).resolve().parent.parent

#: The example site documents every option and is never deployed, so it is not
#: expected to carry the fragments a real machine needs.
DEPLOYED = [p for p in sorted((REPO / "sites").glob("*/*.yaml"))
            if p.parent.name != "example"]
NODE_FILES = DEPLOYED

#: How a fragment names the device it runs. The command is a path ending in
#: "collect", so the quote falls after the word rather than before it.
COMMAND = re.compile(r'collect",\s*\n?\s*"([A-Za-z0-9_]+)"')


def fragments_for(node_path):
    """Fragments belonging to one NODE, not to a whole site.

    A site's nodes have different devices. Installing a site's fragments on
    one node would start collectors for hardware attached to another machine,
    and a test that unions device names across the site cannot see that.
    """
    directory = node_path.parent / "telegraf.d"
    if not directory.is_dir():
        return {}
    prefix = node_path.stem + "-"
    return {p: p.read_text()
            for p in sorted(directory.glob(prefix + "*.conf"))}


def devices_named_in(text):
    return set(COMMAND.findall(text))


def load(path):
    return config.load(path, drivers.names())


@pytest.mark.parametrize("path", NODE_FILES, ids=lambda p: "%s/%s" % (p.parent.name, p.name))
def test_every_resident_device_has_a_stanza_to_run_it(path):
    """A resident device with no execd stanza never runs, and nothing says so."""
    node = load(path)
    wanted = {d.name for d in node.by_mode("resident") if d.enabled}
    if not wanted:
        pytest.skip("this node declares no resident devices")

    covered = set()
    for _, text in fragments_for(path).items():
        covered |= devices_named_in(text)

    missing = wanted - covered
    assert not missing, (
        "%s declares resident device(s) %s that no telegraf fragment runs. "
        "They would silently never report."
        % (path.name, ", ".join(sorted(missing))))


@pytest.mark.parametrize("path", NODE_FILES, ids=lambda p: "%s/%s" % (p.parent.name, p.name))
def test_a_fragment_only_runs_devices_this_node_declares(path):
    """A fragment naming another node's device starts a collector for hardware
    that is not attached to this machine."""
    node = load(path)
    modes = {d.name: d.mode for d in node.devices}

    for fragment, text in fragments_for(path).items():
        named = devices_named_in(text)
        assert named, "%s names no device; the test would pass vacuously" % fragment.name
        for name in named:
            assert name in modes, (
                "%s runs device %r, which %s does not declare"
                % (fragment.name, name, path.name))
            assert modes[name] != "poll", (
                "%s streams %r, but it is a poll device and is already "
                "collected by the exec input" % (fragment.name, name))
            assert modes[name] != "controller", (
                "%s streams %r, but it is a controller device and the "
                "controller owns it" % (fragment.name, name))


@pytest.mark.parametrize("path", NODE_FILES, ids=lambda p: "%s/%s" % (p.parent.name, p.name))
def test_fragments_use_the_installed_paths(path):
    """A fragment referring to a developer's checkout works on a laptop and
    fails on the machine it was written for."""
    for fragment, text in fragments_for(path).items():
        assert "/opt/monitoring/bin/collect" in text, fragment.name
        assert "/etc/monitoring/node.yaml" in text, fragment.name


@pytest.mark.parametrize("path", NODE_FILES, ids=lambda p: "%s/%s" % (p.parent.name, p.name))
def test_resident_stanzas_do_not_ask_the_device_for_a_reading(path):
    """These devices talk when they feel like it; telegraf must read what the
    child prints rather than signal it."""
    for fragment, text in fragments_for(path).items():
        if "inputs.execd" in text:
            assert 'signal = "none"' in text, fragment.name
            assert "restart_delay" in text, (
                "%s: without a restart delay a failing device restarts in a "
                "tight loop" % fragment.name)


def test_the_install_script_removes_its_own_old_fragments():
    """Otherwise a device deleted from a node file leaves its stanza behind
    and keeps being collected."""
    script = (REPO / "deploy" / "install.sh").read_text()
    assert "rm -f /etc/telegraf/telegraf.d/pi-monitoring-*.conf" in script


def test_the_install_script_installs_only_this_node_s_fragments():
    script = (REPO / "deploy" / "install.sh").read_text()
    assert 'telegraf.d/$NODE"-*.conf' in script, (
        "installing a whole site's fragments would start collectors for "
        "hardware attached to a different machine")


def test_the_install_script_checks_devices_as_the_collecting_user():
    """Root can read a repository under a 0700 home directory that telegraf
    cannot, so checking as root would pass and every collection would fail."""
    script = (REPO / "deploy" / "install.sh").read_text()
    assert "runuser -u telegraf" in script


def test_the_install_script_stops_telegraf_before_probing():
    """Its resident children hold the GPIO pins and serial ports; probing
    while they run gives false failures and steals bytes from a live stream."""
    script = (REPO / "deploy" / "install.sh").read_text()
    assert "systemctl stop telegraf" in script


def test_telegraf_is_told_to_log_a_failing_device():
    """Without this telegraf discards the command's stderr whenever the exit
    code is zero, which is the normal case when one device fails and the rest
    succeed. The failure would then appear nowhere at all."""
    conf = (REPO / "deploy" / "telegraf.d" / "monitoring.conf").read_text()
    assert "log_stderr = true" in conf


def test_the_poll_command_has_room_for_a_retrying_sensor():
    """A sensor that retries can take several seconds. telegraf's default
    command timeout is five, and a timeout looks exactly like a dead sensor."""
    conf = (REPO / "deploy" / "telegraf.d" / "monitoring.conf").read_text()
    match = re.search(r'timeout\s*=\s*"(\d+)s"', conf)
    assert match and int(match.group(1)) >= 15
