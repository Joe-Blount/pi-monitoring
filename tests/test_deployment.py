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





def test_the_install_script_installs_only_this_node_s_fragments():
    script = (REPO / "deploy" / "install.sh").read_text()
    assert 'telegraf.d/$NODE"-*.conf' in script, (
        "installing a whole site's fragments would start collectors for "
        "hardware attached to a different machine")








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


INSTALL = (REPO / "deploy" / "install.sh").read_text()
INSTALL_FAST = (REPO / "deploy" / "telegraf.d" / "monitoring.conf").read_text()
INSTALL_SLOW = (REPO / "deploy" / "telegraf.d" / "monitoring-slow.conf").read_text()


def test_the_install_script_has_a_fallback_for_bleak():
    """bleak is not packaged before Debian 12 and one of these machines runs
    11, so apt alone would fail on site with no way to recover there. Both
    sites have an uplink, so pip is a real fallback."""
    assert "pip3 install" in INSTALL


def test_the_install_script_tells_not_needed_apart_from_could_not_decide():
    """One exit code for both meant a broken config skipped bleak silently,
    and the first sign was a device failing with 'needs bleak'."""
    assert "NEED_BLUETOOTH=unknown" in INSTALL
    assert "could not read the node file" in INSTALL


def test_the_install_script_decides_on_bluetooth_from_the_drivers():
    """Matching the node file's text for a name prefix was wrong twice: it
    skipped a Bluetooth driver named after its protocol, and it matched a
    device that was commented out. The script must load the file and ask each
    driver."""
    assert "needs_bluetooth" in INSTALL
    assert "config.load" in INSTALL
    assert "python3-bleak" in INSTALL


def test_a_driver_needing_a_radio_is_reachable_through_that_declaration():
    """If no driver declares it, the install script's check is dead code and
    silently stops installing bleak for anything."""
    declaring = {name for name in drivers.names()
                 if getattr(drivers.get(name), "needs_bluetooth", False)}
    assert declaring, "no driver declares needs_bluetooth"
    assert "abc_bms" in declaring


def test_no_driver_that_needs_a_radio_is_named_for_its_transport_only():
    """A name prefix is not a reliable signal of a Bluetooth driver, which is
    why the install script stopped using one."""
    declaring = {name for name in drivers.canonical()
                 if getattr(drivers.get(name), "needs_bluetooth", False)}
    assert any(not name.startswith("ble_") for name in declaring)


def test_the_install_script_installs_yaml_before_it_reads_a_node_file():
    """The Bluetooth check loads the node file, which needs yaml. On a fresh
    machine the package must already be in place."""
    assert INSTALL.index("python3-yaml") < INSTALL.index("needs_bluetooth")


@pytest.mark.parametrize("path", NODE_FILES, ids=lambda p: "%s/%s" % (p.parent.name, p.name))
def test_the_two_collectors_cover_every_poll_device_exactly_once(path):
    """Two inputs read this node. A device in neither is never read and
    nothing says so; a device in both is read twice, which for a Bluetooth
    pack means two readers contending for one connection."""
    from monitoring import runner

    node = load(path)
    every = {d.name for d in runner.poll_devices(node, "all")}
    fast = {d.name for d in runner.poll_devices(node, "fast")}
    slow = {d.name for d in runner.poll_devices(node, "slow")}

    assert fast | slow == every, "missed: %s" % sorted(every - (fast | slow))
    assert not fast & slow, "read twice: %s" % sorted(fast & slow)


def test_the_fast_collector_asks_only_for_fast_devices():
    """Without the flag it would read the Bluetooth packs too, on the fast
    schedule and the short timeout, which is the failure this split exists to
    prevent."""
    assert "--poll --speed fast" in INSTALL_FAST


def test_the_slow_collector_allows_far_longer_than_the_fast_one():
    """A cold Bluetooth connect alone can take most of a minute."""
    fast = int(re.search(r'timeout = "(\d+)s"', INSTALL_FAST).group(1))
    slow = int(re.search(r'timeout = "(\d+)s"', INSTALL_SLOW).group(1))
    assert slow >= fast * 4, "slow timeout %ds is not enough above %ds" % (slow, fast)


def test_the_slow_collector_runs_far_less_often():
    """These packs allow one connection at a time, so every read locks the
    owner's phone application out for as long as it lasts."""
    fast = int(re.search(r'interval = "(\d+)s"', INSTALL_FAST).group(1))
    slow = int(re.search(r'interval = "(\d+)s"', INSTALL_SLOW).group(1))
    assert slow >= fast * 5


def test_the_slow_collector_is_installed_only_where_it_is_needed():
    assert "has_slow_devices" in INSTALL
    assert "monitoring-slow.conf" in INSTALL


def test_both_collectors_keep_stderr():
    """telegraf discards a command's stderr when the exit code is zero, which
    is the normal case when one device fails and the others succeed."""
    for text in (INSTALL_FAST, INSTALL_SLOW):
        assert "log_stderr = true" in text
