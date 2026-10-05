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


def fragments_for(site):
    directory = REPO / "sites" / site / "telegraf.d"
    if not directory.is_dir():
        return {}
    out = {}
    for path in sorted(directory.glob("*.conf")):
        out[path] = path.read_text()
    return out


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
    for _, text in fragments_for(path.parent.name).items():
        covered |= devices_named_in(text)

    missing = wanted - covered
    assert not missing, (
        "%s declares resident device(s) %s that no telegraf fragment runs. "
        "They would silently never report."
        % (path.name, ", ".join(sorted(missing))))


@pytest.mark.parametrize("site", sorted({p.parent.name for p in NODE_FILES}))
def test_no_stanza_runs_a_device_that_does_not_exist(site):
    """A stanza left behind after a device was removed runs a collector for
    hardware that is no longer declared."""
    known = set()
    for path in (REPO / "sites" / site).glob("*.yaml"):
        known |= {d.name for d in load(path).devices}

    for fragment, text in fragments_for(site).items():
        for name in devices_named_in(text):
            assert name in known, (
                "%s runs device %r, which no node file in %s declares"
                % (fragment.name, name, site))


@pytest.mark.parametrize("site", sorted({p.parent.name for p in NODE_FILES}))
def test_no_stanza_runs_a_poll_device_as_though_it_were_resident(site):
    """Poll devices are collected by the single exec input. Running one as a
    stream as well would publish it twice."""
    modes = {}
    for path in (REPO / "sites" / site).glob("*.yaml"):
        for device in load(path).devices:
            modes[device.name] = device.mode

    for fragment, text in fragments_for(site).items():
        for name in devices_named_in(text):
            assert modes.get(name) != "poll", (
                "%s streams %r, but it is a poll device and is already "
                "collected by the exec input" % (fragment.name, name))


@pytest.mark.parametrize("site", sorted({p.parent.name for p in NODE_FILES}))
def test_fragments_use_the_installed_paths(site):
    """A fragment referring to a developer's checkout works on a laptop and
    fails on the machine it was written for."""
    for fragment, text in fragments_for(site).items():
        for name in devices_named_in(text):
            assert "/opt/monitoring/bin/collect" in text, fragment.name
            assert "/etc/monitoring/node.yaml" in text, fragment.name


@pytest.mark.parametrize("site", sorted({p.parent.name for p in NODE_FILES}))
def test_resident_stanzas_do_not_ask_the_device_for_a_reading(site):
    """These devices talk when they feel like it; telegraf must read what the
    child prints rather than signal it."""
    for fragment, text in fragments_for(site).items():
        if "inputs.execd" in text:
            assert 'signal = "none"' in text, fragment.name
            assert "restart_delay" in text, (
                "%s: without a restart delay a failing device restarts in a "
                "tight loop" % fragment.name)


def test_the_install_script_removes_its_own_old_fragments():
    """Otherwise a device deleted from a node file leaves its stanza behind
    and keeps being collected."""
    script = (REPO / "deploy" / "install.sh").read_text()
    assert "rm -f /etc/telegraf/telegraf.d/monitoring-*.conf" in script


def test_the_install_script_installs_the_node_s_resident_fragments():
    script = (REPO / "deploy" / "install.sh").read_text()
    assert 'sites/$SITE/telegraf.d' in script
