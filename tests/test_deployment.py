"""The node file and the telegraf fragments must agree.

This is the gap that prompted these tests. The blind's node declares two
resident devices, the install script installed only the poll fragment, and
deploying it would have collected temperature while the solar and rain data
simply never appeared. Nothing would have reported an error.

A disagreement between the two must therefore fail here rather than present as
missing data on a machine an hour away.
"""

import os
import pathlib
import re
import subprocess

import pytest

from monitoring import config, drivers
from monitoring.control import location

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








def test_the_poll_command_has_room_for_a_retrying_sensor():
    """A sensor that retries can take several seconds. telegraf's default
    command timeout is five, and a timeout looks exactly like a dead sensor."""
    conf = (REPO / "deploy" / "telegraf.d" / "monitoring.conf").read_text()
    match = re.search(r'timeout\s*=\s*"(\d+)s"', conf)
    assert match and int(match.group(1)) >= 15


INSTALL = (REPO / "deploy" / "install.sh").read_text()

#: The installer with comments removed, whole-line and trailing. Every
#: assertion about what the script does reads this instead. A protection
#: that has been commented out still appears in the raw text, so searching
#: the raw text would accept a script that no longer does the thing.
#:
#: Trailing comments matter as much as whole lines: replacing a command with
#: a no-op and the command text after a hash leaves every naive search
#: satisfied, which is exactly how this was found to be too weak.
def _strip_comments(text):
    out = []
    for line in text.splitlines():
        if "'" in line or '"' in line:
            out.append(line)          # a hash inside a string is not a comment
            continue
        line = line.split("#", 1)[0]
        if line.strip():
            out.append(line)
    return "\n".join(out)


INSTALL_CODE = _strip_comments(INSTALL)
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


def test_a_failing_device_is_visible_without_relying_on_stderr():
    """Telegraf discards a command's stderr when the exit code is zero, which
    is the normal case here because the collector exits zero whenever it wrote
    anything. So the log is not the channel a failure travels on.

    The point is. A failed device publishes ok=0.0 and error_message, which
    reaches the dashboard and outlives a log rotation. This asserts the runner
    still reserves those names, because losing them would make a failure
    genuinely invisible."""
    from monitoring.runner import RESERVED_FIELDS
    assert "ok" in RESERVED_FIELDS
    assert "error_message" in RESERVED_FIELDS


#: Options inputs.exec actually accepts. Telegraf treats an unknown field as a
#: FATAL configuration error: it refuses to start at all, so one bad option in
#: one fragment takes every input on the machine down with it. That is not a
#: hypothetical; it happened, and the node stopped collecting.
EXEC_OPTIONS = {
    "commands", "command", "environment", "timeout", "interval",
    "data_format", "name_override", "name_prefix", "name_suffix",
    "tags", "alias", "precision", "collection_jitter", "collection_offset",
    "ignore_error", "csv_header_row_count", "csv_column_names",
    "csv_column_types", "csv_delimiter", "csv_skip_rows", "csv_tag_columns",
    "csv_timestamp_column", "csv_timestamp_format", "csv_trim_space",
}


@pytest.mark.parametrize("name", ["monitoring.conf", "monitoring-slow.conf"])
def test_the_collector_fragments_use_only_real_exec_options(name):
    """An invented option is not ignored. Telegraf rejects the whole config
    and the service will not start, so the blast radius of a typo here is
    every metric on the machine rather than one input."""
    text = (REPO / "deploy" / "telegraf.d" / name).read_text()
    used = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        used.add(stripped.split("=")[0].strip())
    unknown = used - EXEC_OPTIONS
    assert not unknown, (
        "%s sets %s, which inputs.exec does not accept. Telegraf will refuse "
        "to start." % (name, ", ".join(sorted(unknown))))


def test_the_installer_gives_telegraf_a_writable_home():
    """The telegraf user's home is /etc/telegraf and is not writable, so the
    gpiozero lgpio backend cannot create its notification files and falls back
    to sysfs without saying anything useful."""
    assert "HOME=/var/lib/monitoring" in INSTALL
    assert "WorkingDirectory=/var/lib/monitoring" in INSTALL


def test_the_installer_pins_the_gpio_backend():
    """The sysfs backend does not release a pin when the process dies, so
    every restart finds the pin busy and the collector never recovers. The
    character device backend does release it, which is also the property the
    load-shedding fail-safe depends on, so it is chosen rather than left to a
    silent fallback."""
    assert "GPIOZERO_PIN_FACTORY=lgpio" in INSTALL


def test_the_credentials_file_is_readable_by_the_service_that_needs_it():
    """Owned root:root at mode 600 it is not, and telegraf does not fail
    loudly: it logs one permission line, starts anyway, and runs with no
    output at all. Every input works and nothing reaches the database."""
    doc = (REPO / "docs" / "bringup.md").read_text()
    assert "chown root:telegraf /etc/monitoring/influx.env" in doc
    assert "chmod 640 /etc/monitoring/influx.env" in doc
    assert "chmod 600 /etc/monitoring/influx.env" not in doc


# -- surviving an upgrade --------------------------------------------------

def test_telegraf_is_put_back_however_the_installer_ends():
    """A hang the owner interrupts, a dropped connection, or an out of
    memory kill must not leave the node silent.

    None of those is a command failing, so set -e does not cover them, and
    the script stops telegraf in the middle. Only a trap puts it back.
    """
    script = INSTALL_CODE
    assert "trap 'rc=$?; restore_telegraf; exit $rc' EXIT" in script
    for signal in ("INT", "TERM", "HUP"):
        assert "trap 'exit" in script and signal in script, signal
    # Inside the restore function, not merely somewhere in the file.
    restore = script[script.index("restore_telegraf() {"):]
    restore = restore[:restore.index("\n}")]
    assert "systemctl start telegraf" in restore


#: The block that has to survive a closed stderr on its own: the two
#: TELEGRAF_* flags, both helper functions and all four traps. Extracted from
#: the real script rather than retyped, so a change to the actual code is
#: what the behavioural test below exercises, not a hand-written stand-in for
#: it.
def _trap_machinery():
    start = INSTALL.index("TELEGRAF_WAS_RUNNING=0")
    end = INSTALL.index("trap 'exit 129' HUP") + len("trap 'exit 129' HUP")
    return INSTALL[start:end]


def _telegraf_restarts_with_stderr(tmp_path, stderr_closed):
    """Run the installer's real trap machinery to the point a dropped ssh
    connection would drive it to, and report whether telegraf was asked to
    start again.

    A fake systemctl stands in for the real one: it reports telegraf as not
    active, same as after stop_telegraf_once, and records a start rather than
    doing one. Closing stderr before exiting reproduces what a dropped
    connection does to this process before the HUP trap ever runs; leaving it
    open is the control that shows the same machinery working when nothing
    is wrong.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "telegraf-started"
    fake_systemctl = bin_dir / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1 $2\" == 'is-active --quiet' ]]; then exit 1; fi\n"
        "if [[ \"$1\" == 'start' ]]; then touch '%s'; exit 0; fi\n"
        "exit 0\n" % marker)
    fake_systemctl.chmod(0o755)

    harness = tmp_path / "harness.sh"
    harness.write_text("\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        _trap_machinery(),
        "TELEGRAF_STOPPED=1",
        "exec 2>&-" if stderr_closed else ":",
        "exit 129",
    ]) + "\n")
    harness.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    subprocess.run(["bash", str(harness)], env=env,
                   capture_output=True, text=True)
    return marker.exists()


def test_telegraf_is_restarted_even_when_stderr_is_already_closed(tmp_path):
    """A dropped ssh connection closes this process's stderr before the HUP
    trap ever runs. The diagnostic echo in restore_telegraf can then fail on
    its own account, with nothing wrong with telegraf at all, and that
    failure must not be the reason the restart is skipped: once telegraf has
    been stopped by this script, nothing short of the kernel may keep it
    from starting again."""
    assert _telegraf_restarts_with_stderr(tmp_path, stderr_closed=True)


def test_telegraf_is_restarted_when_stderr_is_open_too(tmp_path):
    """The control case: the same trap machinery restarts telegraf when
    nothing about the connection has failed, so the closed-stderr case above
    is isolating the one thing that changed."""
    assert _telegraf_restarts_with_stderr(tmp_path, stderr_closed=False)


def _telegraf_restarts_with_a_dead_pipe(tmp_path, keep_pipe_trap):
    """Run the installer's real trap machinery with its output going to a
    pipe whose reader has already gone, and report whether telegraf was
    asked to start again.

    This is what a connection dropped without a terminal leaves behind:
    stdout is a pipe, the far end has exited, and the next progress line
    raises SIGPIPE. The default action for that signal kills the shell
    outright, so no trap runs at all and telegraf stays stopped on a machine
    nobody is standing next to. Ignoring the signal turns the same write
    into an error, which errexit unwinds through the EXIT trap.

    `keep_pipe_trap` false strips the ignore out of the real machinery, so
    this test measures the ignore rather than the harness.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "telegraf-started"
    fake_systemctl = bin_dir / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1 $2\" == 'is-active --quiet' ]]; then exit 1; fi\n"
        "if [[ \"$1\" == 'start' ]]; then touch '%s'; exit 0; fi\n"
        "exit 0\n" % marker)
    fake_systemctl.chmod(0o755)

    machinery = _trap_machinery()
    if not keep_pipe_trap:
        machinery = "\n".join(line for line in machinery.splitlines()
                               if "trap '' PIPE" not in line)
    else:
        assert "trap '' PIPE" in machinery, (
            "the installer does not ignore SIGPIPE, so this test is "
            "measuring nothing")

    harness = tmp_path / "harness.sh"
    harness.write_text("\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        machinery,
        "TELEGRAF_STOPPED=1",
        # Progress lines, as the installer prints many of. The first one
        # reaching a dead pipe is where the script ends either way; the
        # question is whether it ends through the trap or through the signal.
        "echo '==> doing work'",
        "echo '==> more work'",
        "exit 0",
    ]) + "\n")
    harness.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    # head -0 reads nothing and exits at once, which is what leaves the
    # pipe's reader gone while the writer is still running.
    subprocess.run(
        ["bash", "-c", "'%s' 2>&1 | head -c 0" % harness],
        env=env, capture_output=True, text=True)
    return marker.exists()


def test_telegraf_is_restarted_when_the_far_end_of_the_output_has_gone(
        tmp_path):
    """A connection dropped without a terminal leaves stdout a pipe with no
    reader, and the default action for SIGPIPE kills the shell before any
    trap can run. Telegraf would then stay stopped, and the node silent,
    until somebody drives out to it."""
    assert _telegraf_restarts_with_a_dead_pipe(tmp_path, keep_pipe_trap=True)


def test_a_dead_output_pipe_would_otherwise_skip_the_restart(tmp_path):
    """The control for the test above: with the signal left at its default,
    the same machinery does not restart telegraf. Without this, a harness
    that never actually produced the signal would make the test above pass
    whatever the installer did."""
    assert not _telegraf_restarts_with_a_dead_pipe(tmp_path,
                                                   keep_pipe_trap=False)


def test_every_package_install_goes_through_a_helper_that_stops_telegraf():
    """apt beside a Bluetooth scan is the one recorded way this script has
    locked up a machine, and a locked machine needs a visit.

    Checked structurally rather than by ordering lines. Ordering passes as
    long as some earlier stop exists anywhere, which a call added later in a
    different branch would satisfy while still running beside a collector.
    Routing every install through one of two helpers cannot be forgotten.
    """
    lines = INSTALL_CODE.splitlines()
    installers = [l for l in lines
                  if "apt-get install" in l or "pip3 install --quiet" in l]
    assert len(installers) == 2, (
        "expected exactly the two helper bodies, found: %s" % installers)

    for helper in ("install_from_apt()", "install_from_pip()"):
        start = next(i for i, l in enumerate(lines) if l.startswith(helper))
        # To the closing brace, not a fixed number of lines. A window that
        # runs past the end finds the next function's stop and passes.
        end = next(i for i, l in enumerate(lines[start:], start) if l == "}")
        body = lines[start:end]
        assert any("stop_telegraf_once" in l for l in body), helper
        assert any("apt-get install" in l or "pip3 install" in l
                   for l in body), helper


def test_the_heaviest_package_path_is_refused_when_memory_is_short():
    script = INSTALL_CODE
    assert "MemAvailable" in script, "free memory is the wrong number to read"
    # The comparison itself, inside the helper that runs pip, not just a
    # constant defined somewhere and never consulted.
    helper = script[script.index("install_from_pip() {"):]
    helper = helper[:helper.index("\n}")]
    assert "available_mb" in helper and "PIP_MIN_AVAILABLE_MB" in helper
    assert "return" in helper, "the guard must stop pip, not just measure"


def test_the_device_check_cannot_run_for_ever_with_telegraf_stopped():
    """Most drivers here have no deadline of their own."""
    script = INSTALL_CODE
    assert "timeout --foreground \"$CHECK_TIMEOUT\" runuser" in script, (
        "without --foreground the timeout runs in its own process group and "
        "an operator's interrupt never reaches what is stuck")
    # Captured, not read after a negation. After `if ! cmd` the status is
    # the negation's, always zero, so the branch explaining a timeout could
    # never run and a killed check reported itself as a dead sensor.
    assert "|| check_status=$?" in script
    assert "if ! timeout" not in script


def test_every_fragment_name_this_project_has_ever_written_is_removed():
    """Rolling back across the rename would otherwise start two execd
    children for one device, which fight over the port and restart for ever.
    """
    script = INSTALL_CODE
    for pattern in ("pi-monitoring.conf", "pi-monitoring-*.conf",
                    "monitoring.conf", "monitoring-*.conf"):
        assert "rm -f /etc/telegraf/telegraf.d/%s" % pattern in script, pattern


def test_the_installer_says_which_commit_it_installed():
    """A pull that failed still lets the install report success."""
    script = INSTALL_CODE
    assert "rev-parse --short HEAD" in script
    assert "status --porcelain" in script


def test_the_credentials_example_agrees_with_the_bringup_guide():
    """Owned root:root at mode 600, telegraf starts and sends nothing.

    The guide explains that failure, so the example has to agree with it. An
    example that contradicts the guide is how somebody following the
    instructions installs the failure the guide exists to prevent.
    """
    example = (REPO / "influx.env.example").read_text()
    assert "chown root:telegraf" in example
    assert "chmod 640" in example
    assert "chmod 600" not in example
    assert "chown root:root" not in example


def test_no_package_helper_is_called_bare_under_set_e():
    """set -e ends the script on a non-zero return from a bare command.

    The pip helper returns a distinct status to mean "refused, not enough
    memory", so that the installer can say so and carry on. Called bare,
    that return kills the script instead, on exactly the small machine the
    refusal exists to protect. Every call must be guarded by ||, &&, or an
    if, so the status reaches the code that reports it.
    """
    # Continuations first. A call whose guard sits on the next line after a
    # backslash is guarded, and reading the lines separately would say
    # otherwise.
    joined, pending = [], ""
    for line in INSTALL_CODE.splitlines():
        pending += line.rstrip()
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        joined.append(pending)
        pending = ""
    if pending:
        joined.append(pending)

    checked = 0
    for statement in joined:
        stripped = statement.strip()
        if not stripped.startswith(("install_from_apt", "install_from_pip")):
            continue
        if stripped.endswith("() {"):
            continue                       # the definition, not a call
        checked += 1
        assert "||" in stripped or "&&" in stripped, (
            "unguarded under set -e: %s" % stripped)
    assert checked, "found no calls to check, so this proves nothing"


# -- the README must describe what actually exists --------------------------

def _described_drivers():
    """Driver names in the first column of the README table.

    The first column rather than the section text, because rows refer to
    each other: one row pointing at another driver by name would otherwise
    stand in for that driver's own row and a deleted row would pass.
    """
    readme = (REPO / "README.md").read_text()
    table = readme[readme.index("## Drivers"):readme.index("## Status")]
    return set(re.findall(r"^\| `([a-z0-9_]+)`", table, re.MULTILINE))


def test_the_readme_lists_every_driver_that_exists():
    """Prose goes stale silently, and a reader has no way to tell.

    This is the cheapest way to hold the two together: a driver that nobody
    described fails here until somebody describes it.
    """
    missing = drivers.canonical() - _described_drivers()
    assert not missing, (
        "registered but not described in the README: %s" % sorted(missing))


def test_the_readme_does_not_describe_drivers_that_are_gone():
    unknown = _described_drivers() - drivers.names()
    assert not unknown, (
        "the README describes drivers that do not exist: %s" % sorted(unknown))


# -- installing the controller ----------------------------------------------

def test_the_install_script_installs_the_unit_and_the_tmpfiles_entry():
    """Installed on every node, started only where the node file asks. The
    files cost nothing idle, and having them in place is what makes enabling
    control later an edit rather than another visit."""
    assert "monitoring-control.service" in INSTALL_CODE
    assert "/etc/systemd/system/monitoring-control.service" in INSTALL_CODE
    assert "tmpfiles-monitoring.conf" in INSTALL_CODE
    assert "/etc/tmpfiles.d/" in INSTALL_CODE


def test_the_install_script_creates_the_runtime_directories_immediately():
    """The unit's writable paths must exist before it starts, and this
    script may be the thing starting it moments later. Waiting for the next
    boot would mean the first install never brings the controller up."""
    assert "systemd-tmpfiles --create" in INSTALL_CODE


def test_a_runtime_directory_that_cannot_be_created_is_reported():
    """Every collector on a node with a control block writes the reading
    cache under that path, so a path telegraf cannot create becomes one
    complaint per poll process with load control deciding from nothing.

    Not fatal, because a software update must never be blocked on a machine
    nobody can reach. But the install has already seen the cause, and
    swallowing it leaves whoever reads those complaints later with nothing to
    connect them to.
    """
    section = INSTALL_CODE[INSTALL_CODE.index("systemd-tmpfiles --create"):]
    section = section[:section.index("systemctl daemon-reload")]
    assert "WARNING" in section, (
        "a failure to create the runtime directories is swallowed")
    assert "exit 1" not in section, (
        "a runtime directory blocked the install")


def test_the_install_script_reloads_systemd_after_writing_a_unit():
    """A unit file written and not reloaded is a unit systemd does not know
    about, and the start a few lines later fails with a message about the
    name being wrong."""
    unit = INSTALL_CODE.index("monitoring-control.service")
    reload_after = INSTALL_CODE.index("systemctl daemon-reload", unit)
    start = INSTALL_CODE.index("systemctl restart monitoring-control")
    assert reload_after < start, (
        "the unit is started before systemd has been told it exists")


def test_the_install_script_asks_the_node_file_whether_control_is_wanted():
    """Read with the loader rather than matched in the text, the same way the
    Bluetooth check is. Matching counts a commented-out block, and the word
    appears in comments."""
    wanted = INSTALL_CODE.index("CONTROL_WANTED")
    assert "config.load" in INSTALL_CODE[wanted:wanted + 900]
    assert "node.control" in INSTALL_CODE[wanted:wanted + 900]


def test_the_install_script_stops_the_controller_when_control_is_switched_off():
    """Disabling alone leaves a running controller driving relays from the
    block it started with, which is the opposite of what switching control
    off in a node file is asking for."""
    assert "systemctl stop monitoring-control" in INSTALL_CODE


def test_a_controller_that_will_not_start_does_not_fail_the_install():
    """The same rule as a dead sensor: a five dollar part, or one service,
    must never block a software update on a machine nobody can reach. Every
    load sits at its wired fail-safe while the controller is down."""
    section = INSTALL_CODE[INSTALL_CODE.index("monitoring-control is running"):]
    section = section[:section.index("echo \"==> restarting telegraf\"")]
    assert "exit 1" not in section, (
        "a controller that will not start aborts the install")


def test_the_install_script_writes_a_location_template_and_never_values():
    """A guessed location produces a schedule wrong in a way nothing
    downstream can detect, showing up months later as cameras keeping hours
    nobody asked for."""
    assert "location.yaml.example" in INSTALL_CODE
    assert "/etc/monitoring/location.yaml" in INSTALL_CODE
    assert "TEMPLATE" in INSTALL


def test_the_install_script_never_overwrites_an_existing_location():
    """This script runs again on every update, and overwriting it would undo
    the one thing a person had to type."""
    section = INSTALL_CODE[INSTALL_CODE.index("where this machine is"):]
    section = section[:section.index("node file")]
    assert "-e /etc/monitoring/location.yaml" in section, (
        "nothing checks whether the file already exists")
    assert "left alone" in INSTALL


def test_the_location_example_is_not_a_real_site():
    """It ships in a public repository, so it must be a rounded nearby town
    rather than anybody's coordinates."""
    example = (REPO / "location.yaml.example").read_text()
    for line in example.splitlines():
        for key in ("latitude:", "longitude:"):
            if line.startswith(key):
                value = line.split(":", 1)[1].strip()
                assert len(value.split(".")[-1]) <= 1, (
                    "%s is given to more than a tenth of a degree, which is "
                    "more precision than an example should carry" % line)


def test_the_installed_location_template_makes_location_read_refuse(tmp_path):
    """The example commits numbers so its own comments still make sense read
    on their own, but what the installer actually puts on a machine must not
    be a usable point: location.py's whole "refused, never defaulted" rule
    means a schedule computed against an invented place is wrong in a way
    nothing downstream can detect.

    Run the installer's own transform rather than a copy of it, on the real
    example, and feed the result to the real location.read(): a change to
    the sed expression that stops commenting out latitude and longitude
    would pass a string-only check but must fail here.
    """
    match = re.search(r"sed -E '([^']*)'", INSTALL)
    assert match, "no sed transform found in the install script's location step"

    written = tmp_path / "location.yaml"
    with open(written, "wb") as out:
        subprocess.run(
            ["sed", "-E", match.group(1), str(REPO / "location.yaml.example")],
            stdout=out, check=True)

    with pytest.raises(location.LocationError) as raised:
        location.read(str(written))
    assert "latitude" in str(raised.value)


def test_an_unusable_location_file_is_not_reported_as_left_alone():
    """Printing the same "exists, left alone" line for a template nobody has
    touched and for a file somebody actually filled in means an operator
    reading the install output can never tell which one a machine has, until
    a schedule shows up wrong months later.

    The question is answered by the loader that will really read the file,
    not by searching its text. A file where somebody uncommented one of the
    two coordinates matches any marker test that can be written and is still
    refused at start, and the installer would have called it ready.
    """
    section = INSTALL_CODE[INSTALL_CODE.index("where this machine is"):]
    section = section[:section.index("node file")]
    exists_branch = section[section.index("-e /etc/monitoring/location.yaml"):]
    assert "location_refusal" in exists_branch, (
        "the exists-branch does not ask the loader whether the file is "
        "usable, so an installed-but-unedited template is indistinguishable "
        "from a real one in the install output")
    assert "location.read" in section, (
        "the probe has to go through the real reader, or it is a second "
        "opinion about what the file means")


def test_the_location_probe_refuses_a_half_edited_file(tmp_path):
    """One coordinate filled in and the other left commented out.

    This is the likely mistake: somebody edits the file at the machine, is
    interrupted, and comes back to a file that looks edited. It has to be
    refused by name, saying which of the two is missing, because the
    alternative is a schedule placed on a meridian seven degrees from this
    one, which is half an hour of error in every sunrise.
    """
    example = (pathlib.Path(__file__).resolve().parent.parent
               / "location.yaml.example").read_text()
    half = pathlib.Path(tmp_path) / "location.yaml"
    half.write_text(example.replace("longitude:", "# longitude:"))
    with pytest.raises(location.LocationError) as raised:
        location.read(str(half))
    assert "needs a longitude" in str(raised.value)
