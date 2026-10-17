"""bin/control, exercised as the real binary wherever that is possible.

Most of this runs the script as a subprocess, the way tests/test_runner.py
tests bin/collect's own argument handling and exit codes: the behaviour
under test is the process's, not a function's.

Two things have no black-box proxy and are tested through a direct import
instead, in the same spirit as test_relay.py's mock-factory tests and
test_runner.py's lock test: a loop_interval default has nothing to read
back from a short-lived process, and a relay closing on shutdown produces
no observable difference on a machine with no GPIO hardware to leave
claimed. Both are marked where they appear.

Every node file here declares a real GPIO pin for its load. That is
deliberate rather than an oversight: nothing in this suite ever gives the
controller a reading to act on, so Loop._apply never calls a relay's own
apply() (see loop.py's own rule that nothing is driven before a reading
informs a decision), and the pin number is never actually touched. That is
exactly what lets `once` and `run` be tested on a machine with no lgpio and
no hardware at all: if that rule ever broke, these tests would start
failing with a RelayError instead of passing quietly.
"""

import datetime
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import zoneinfo

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROL = REPO / "bin" / "control"

#: Injected the same way tests/test_control_loop.py injects one: the real
#: identity comes from /proc, which does not exist on every machine this
#: suite runs on.
BOOT = "test-boot-identity"

NODE_HEADER = """\
site: test
node: test
measurement: test
devices:
  - {name: bms, driver: host, mode: poll}
"""

#: Where the machine is, in its own file rather than in the node file,
#: because node files are committed and this repository is public. The
#: binary reads it from MONITORING_LOCATION, which every run below sets.
LOCATION = """\
latitude: 37.0
longitude: -97.4
timezone: America/Chicago
"""


def location_file(tmp_path, body=LOCATION):
    path = tmp_path / "location.yaml"
    path.write_text(body)
    return path

LADDER = """\
      shed_ladder:
        source: {device: bms, field: state_of_charge_pct}
        stale_after: 10m
        release_margin_pct: 15
        enter_after_readings: 1
        restore_dwell: 0
        tiers:
          - below_pct: 50
            schedule: {type: clock, on_from: "05:00", on_to: "22:00"}
          - {below_pct: 10, schedule: {type: never}}
"""

#: A ladder with one solar tier rather than one clock tier, matching the
#: shape of the 25 percent tier in sites/blind1/downstairs.yaml. Exists so a
#: dry-run test can latch a tier whose schedule is measured from sunrise and
#: sunset rather than the clock, which is the one shape the clock-only
#: ladder above never exercises.
SOLAR_LADDER = """\
      shed_ladder:
        source: {device: bms, field: state_of_charge_pct}
        stale_after: 10m
        release_margin_pct: 15
        enter_after_readings: 1
        restore_dwell: 0
        tiers:
          - below_pct: 25
            schedule:
              type: solar
              on_windows:
                - {from: [sunrise, -1h], to: [sunrise, 2h]}
                - {from: [sunset, -2h], to: [sunset, 1h]}
"""


def controlled_node(tmp_path, loop_interval=None, polarity="energize_to_shed",
                    ladder=LADDER):
    """A node with one real load, a real GPIO pin, and no reading for it.

    No loop_interval line at all when `loop_interval` is None, so a test can
    tell the default apart from a value that happens to match it. `ladder`
    defaults to the clock-tier fixture above; a test that needs a solar tier
    latched passes SOLAR_LADDER instead.
    """
    body = NODE_HEADER + "control:\n  enabled: true\n"
    if loop_interval is not None:
        body += "  loop_interval: %s\n" % loop_interval
    body += "  loads:\n    cameras:\n      type: dc_branch\n"
    body += "      control_pin: 23\n      polarity: %s\n" % polarity
    body += ladder
    path = tmp_path / "node.yaml"
    path.write_text(body)
    return path


def empty_node(tmp_path, loop_interval=None):
    """Control declared but switched off, and no load at all."""
    body = NODE_HEADER + "control:\n  enabled: false\n"
    if loop_interval is not None:
        body += "  loop_interval: %s\n" % loop_interval
    path = tmp_path / "node.yaml"
    path.write_text(body)
    return path


def bad_polarity_node(tmp_path):
    body = NODE_HEADER + "control:\n  enabled: true\n"
    body += "  loads:\n    cameras:\n      type: dc_branch\n"
    body += "      control_pin: 23\n      polarity: sideways\n"
    body += LADDER
    path = tmp_path / "node.yaml"
    path.write_text(body)
    return path


#: The system zone every one of these subprocesses runs in, deliberately not
#: the site's.
#:
#: The commands here read and print expiries, and they must do it against the
#: zone in the location file rather than the machine's own. On a developer's
#: machine already set to the site's zone the two agree, so code that reads
#: the wrong one passes every test and fails only on the Pi, which is left at
#: the Debian default. Pinning a third zone here means neither the site's zone
#: nor a bare UTC default can be mistaken for correct.
SYSTEM_ZONE = "Asia/Tokyo"


def run_control(tmp_path, *args, extra_env=None, timeout=30):
    env = dict(os.environ)
    env["TZ"] = SYSTEM_ZONE
    env["MONITORING_STATE"] = str(tmp_path / "state.json")
    env["MONITORING_PUBLISHED"] = str(tmp_path / "published.json")
    env["MONITORING_HIGH_WATER"] = str(tmp_path / "hw.json")
    env["MONITORING_BOOT_ID"] = BOOT
    env.setdefault("MONITORING_LOCATION",
                   str(location_file(tmp_path)) if not (
                       tmp_path / "location.yaml").exists()
                   else str(tmp_path / "location.yaml"))
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(CONTROL)] + list(args),
        capture_output=True, text=True, timeout=timeout, env=env)


def read_published(tmp_path):
    with open(tmp_path / "published.json") as handle:
        return json.load(handle)


# -- the node path: a default, an env var, and the flag that beats both ----

def test_the_default_node_path_matches_what_this_binary_documents(tmp_path):
    """Not a real file on this or any dev machine, so loading it fails, and
    the failure names the path this binary tried -- proof the compiled-in
    default was actually used rather than something else silently filled
    in."""
    env = dict(os.environ)
    env.pop("MONITORING_NODE", None)
    result = run_control(tmp_path, "once", extra_env=env)
    assert result.returncode == 2
    assert "/etc/monitoring/node.yaml" in result.stderr


def test_the_environment_variable_overrides_the_default_path(tmp_path):
    bogus = str(tmp_path / "nowhere.yaml")
    result = run_control(tmp_path, "once",
                         extra_env={"MONITORING_NODE": bogus})
    assert result.returncode == 2
    assert bogus in result.stderr


def test_the_node_flag_overrides_the_environment_variable(tmp_path):
    node = controlled_node(tmp_path)
    result = run_control(
        tmp_path, "once", "--node", str(node),
        extra_env={"MONITORING_NODE": str(tmp_path / "nowhere.yaml")})
    assert result.returncode == 0, result.stderr


# -- run refuses a bad control block rather than starting -------------------

def test_run_refuses_a_bad_polarity_and_does_not_start(tmp_path):
    """A SpecError. Starting anyway would drive the pin from a default
    nobody wrote, since the polarity is exactly what decides what a dead
    controller leaves behind."""
    node = bad_polarity_node(tmp_path)
    result = run_control(tmp_path, "run", "--node", str(node))
    assert result.returncode == 2
    assert "polarity" in result.stderr
    assert "Traceback" not in result.stderr


def test_run_refuses_an_unreadable_node_file(tmp_path):
    """A ConfigError, the other of the two cases a control block can fail
    in."""
    missing = tmp_path / "missing.yaml"
    result = run_control(tmp_path, "run", "--node", str(missing))
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_run_refuses_a_disabled_node_and_does_not_start(tmp_path):
    """`enabled: false` is somebody saying this machine does not control
    anything, and that has to be told to whoever is standing at it without
    first demanding a location file: the refusal comes before the location
    is ever read, and names the key that would change it."""
    node = empty_node(tmp_path)
    result = run_control(tmp_path, "run", "--node", str(node))
    assert result.returncode == 2
    assert "control.enabled" in result.stderr
    assert "Traceback" not in result.stderr


def test_once_refuses_a_disabled_node(tmp_path):
    node = empty_node(tmp_path)
    result = run_control(tmp_path, "once", "--node", str(node))
    assert result.returncode == 2
    assert "control.enabled" in result.stderr
    assert "Traceback" not in result.stderr


def test_run_refuses_a_machine_with_no_location_file(tmp_path):
    """A solar schedule computed against an invented point is wrong in a way
    nothing downstream can detect, so this is required rather than
    defaulted, and a machine where nobody wrote the file must say which file
    and what goes in it rather than run on a guess."""
    node = controlled_node(tmp_path)
    result = run_control(tmp_path, "run", "--node", str(node),
                         extra_env={"MONITORING_LOCATION":
                                    str(tmp_path / "absent.yaml")})
    assert result.returncode == 2
    assert "absent.yaml" in result.stderr
    assert "location.yaml.example" in result.stderr, (
        "the message must say what to copy")
    assert "Traceback" not in result.stderr


def test_run_refuses_an_unknown_timezone(tmp_path):
    node = controlled_node(tmp_path)
    path = location_file(tmp_path, LOCATION.replace("America/Chicago",
                                                    "Nowhere/Fake"))
    result = run_control(tmp_path, "run", "--node", str(node),
                         extra_env={"MONITORING_LOCATION": str(path)})
    assert result.returncode == 2
    assert "Nowhere/Fake" in result.stderr


def test_run_refuses_degrees_and_minutes_written_as_a_decimal(tmp_path):
    """Not a refusal this can make in general, because the mistaken form is
    itself a valid latitude. What it can do is name the mistake, since
    writing degrees and minutes where degrees and decimals belong is the
    error somebody converting by hand actually makes, and at this latitude
    it is a couple of miles of it.

    The worked example in that message is deliberately nowhere near this
    site. A number carrying part of a real position publishes it whatever
    notation it is written in."""
    node = controlled_node(tmp_path)
    path = location_file(tmp_path, "latitude: ninety\nlongitude: -97.4\n"
                                   "timezone: America/Chicago\n")
    result = run_control(tmp_path, "run", "--node", str(node),
                         extra_env={"MONITORING_LOCATION": str(path)})
    assert result.returncode == 2
    assert "degrees and decimals" in result.stderr.lower()


def test_run_refuses_a_location_with_a_key_nobody_reads(tmp_path):
    """The same rule the control spec applies: a key accepted and unread
    sits in the file looking like it is in force."""
    node = controlled_node(tmp_path)
    path = location_file(tmp_path, LOCATION + "elevation: 350\n")
    result = run_control(tmp_path, "run", "--node", str(node),
                         extra_env={"MONITORING_LOCATION": str(path)})
    assert result.returncode == 2
    assert "unknown key" in result.stderr


def test_once_and_dry_run_refuse_the_same_bad_block_as_run(tmp_path):
    """The refusal is not special to one subcommand."""
    node = bad_polarity_node(tmp_path)
    for subcommand in ("once", "dry-run"):
        result = run_control(tmp_path, subcommand, "--node", str(node))
        assert result.returncode == 2, subcommand
        assert "polarity" in result.stderr, subcommand


# -- once and dry-run print one line per load --------------------------------

def test_once_prints_the_load_name_connection_tier_and_reason(tmp_path):
    node = controlled_node(tmp_path)
    result = run_control(tmp_path, "once", "--node", str(node))
    assert result.returncode == 0, result.stderr
    assert "cameras:" in result.stdout
    assert "tier" in result.stdout
    # No reading ever arrives in this suite, so the ladder has nothing
    # latched and the load holds at its fail-safe-informed default.
    assert "connected" in result.stdout or "disconnected" in result.stdout


def test_once_runs_a_pass_that_writes_the_state_file(tmp_path):
    node = controlled_node(tmp_path)
    result = run_control(tmp_path, "once", "--node", str(node))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state.json").exists()


def test_dry_run_prints_a_line_per_load_too(tmp_path):
    node = controlled_node(tmp_path)
    result = run_control(tmp_path, "dry-run", "--node", str(node))
    assert result.returncode == 0, result.stderr
    assert "cameras:" in result.stdout
    assert "tier" in result.stdout


def test_dry_run_does_not_write_the_state_file(tmp_path):
    """`persist=False` has to reach every file a real pass leaves behind:
    the loop's own working state, the document published for a dashboard
    to read, and the clock's high-water mark. Looking at a node file must
    never leave a trace that a pass actually ran."""
    node = controlled_node(tmp_path)
    result = run_control(tmp_path, "dry-run", "--node", str(node))
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "state.json").exists(), (
        "dry-run wrote the loop's own state file")
    assert not (tmp_path / "published.json").exists(), (
        "dry-run published a document meant for a real pass")
    assert not (tmp_path / "hw.json").exists(), (
        "dry-run advanced the clock's high-water mark")


# -- dry-run touches no GPIO, and says what it would have done --------------

def test_dry_run_never_crashes_reaching_a_real_pin(tmp_path):
    """The node declares a real control_pin and this machine has no lgpio
    and no GPIO hardware at all. If dry-run ever called apply() on a real
    relay it would raise a RelayError here, uncaught, and this test would
    see a non-zero exit and a traceback instead of a clean report."""
    node = controlled_node(tmp_path)
    result = run_control(tmp_path, "dry-run", "--node", str(node))
    assert result.returncode == 0
    assert result.stderr == ""


def test_dry_run_reports_unrestricted_when_no_tier_is_latched(tmp_path):
    node = controlled_node(tmp_path)
    result = run_control(tmp_path, "dry-run", "--node", str(node))
    assert result.returncode == 0, result.stderr
    assert "unrestricted" in result.stdout


def test_dry_run_prints_todays_on_periods_for_a_latched_tier(tmp_path):
    """The requirement this exists for: the schedule has to be visible
    rather than inferred from one decision made at one moment. A latched
    clock tier of 05:00-22:00 must show that window, not just today's single
    connected-or-not verdict."""
    node = controlled_node(tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "boot_id": BOOT,
        "loads": {"cameras": {"latched": {"0": True}, "below": {},
                              "above_since": {}, "floor_latched": False,
                              "floor_latched_by": None}}}))
    result = run_control(tmp_path, "dry-run", "--node", str(node))
    assert result.returncode == 0, result.stderr
    assert "05:00-22:00" in result.stdout


def test_dry_run_prints_off_all_day_for_a_latched_never_tier(tmp_path):
    node = controlled_node(tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "boot_id": BOOT,
        "loads": {"cameras": {"latched": {"0": True, "1": True}, "below": {},
                              "above_since": {}, "floor_latched": False,
                              "floor_latched_by": None}}}))
    result = run_control(tmp_path, "dry-run", "--node", str(node))
    assert result.returncode == 0, result.stderr
    assert "off all day" in result.stdout


def test_dry_run_reports_real_output_for_a_latched_solar_tier(tmp_path):
    """A latched tier whose schedule is measured from sunrise and sunset
    has to be walked by the same preview that already walks a latched
    clock tier, and has to come back with a real report rather than a
    traceback."""
    node = controlled_node(tmp_path, ladder=SOLAR_LADDER)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "boot_id": BOOT,
        "loads": {"cameras": {"latched": {"0": True}, "below": {},
                              "above_since": {}, "floor_latched": False,
                              "floor_latched_by": None}}}))
    result = run_control(tmp_path, "dry-run", "--node", str(node))
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert "cameras:" in result.stdout
    assert "today:" in result.stdout


# -- run handles SIGTERM and SIGINT cleanly ----------------------------------

def _start_run(tmp_path, node, extra_env=None):
    env = dict(os.environ)
    env["MONITORING_STATE"] = str(tmp_path / "state.json")
    env["MONITORING_PUBLISHED"] = str(tmp_path / "published.json")
    env["MONITORING_HIGH_WATER"] = str(tmp_path / "hw.json")
    env["MONITORING_BOOT_ID"] = BOOT
    env.setdefault("MONITORING_LOCATION",
                   str(location_file(tmp_path)) if not (
                       tmp_path / "location.yaml").exists()
                   else str(tmp_path / "location.yaml"))
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, str(CONTROL), "run", "--node", str(node)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for_a_pass(tmp_path, proc, timeout=20.0):
    """Block until the loop has published a pass, and return how many.

    A published document is the loop's own statement that it is running: it
    is written at the end of a pass, which is after the signal handlers are
    installed. Waiting for it rather than for a fixed stretch of wall clock
    is what keeps these tests from depending on how loaded the machine is.
    A fixed wait is a guess about somebody else's scheduler, and the way it
    fails is a signal delivered before the handler exists, which kills the
    process with the default action and reads as the behaviour under test
    being broken.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            pytest.fail("run exited before publishing a pass: %s" % err)
        try:
            return read_published(tmp_path)["fields"]["passes"]
        except (OSError, ValueError, KeyError):
            time.sleep(0.02)
    proc.kill()
    proc.communicate()
    pytest.fail("run published no pass within %gs" % timeout)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_run_exits_cleanly_on_a_shutdown_signal(tmp_path, sig):
    node = controlled_node(tmp_path, loop_interval="50ms")
    proc = _start_run(tmp_path, node)
    _wait_for_a_pass(tmp_path, proc)
    proc.send_signal(sig)
    try:
        out, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("run did not exit within 5s of the signal")
    assert proc.returncode == 0, err
    assert "Traceback" not in err


def test_run_exits_promptly_rather_than_at_the_end_of_an_interval(tmp_path):
    """A loop_interval long enough that only a prompt signal, not the
    interval elapsing on its own, explains an exit this fast."""
    node = controlled_node(tmp_path, loop_interval="10s")
    proc = _start_run(tmp_path, node)
    _wait_for_a_pass(tmp_path, proc)
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("run waited out its loop_interval instead of the signal")
    assert time.monotonic() - started < 3


# -- the loop interval: from the file, and its default -----------------------

def test_a_short_loop_interval_from_the_node_file_is_honoured(tmp_path):
    """A rate, so this one does need a stretch of wall clock. The stretch is
    measured from the loop's first published pass rather than from the
    process starting, so a slow start borrows no time from it."""
    node = controlled_node(tmp_path, loop_interval="50ms")
    proc = _start_run(tmp_path, node)
    first = _wait_for_a_pass(tmp_path, proc)
    time.sleep(0.45)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=5)
    passes = read_published(tmp_path)["fields"]["passes"]
    assert passes - first >= 3, (
        "only %d further pass(es) in 0.45s at a 50ms interval"
        % (passes - first))


def test_with_no_loop_interval_in_the_file_only_one_pass_runs_quickly(
        tmp_path):
    """The default is 30s. So the one pass every start makes before it ever
    sleeps must be the only one, however long this waits after it."""
    node = controlled_node(tmp_path, loop_interval=None)
    proc = _start_run(tmp_path, node)
    assert _wait_for_a_pass(tmp_path, proc) == 1
    time.sleep(0.4)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=5)
    passes = read_published(tmp_path)["fields"]["passes"]
    assert passes == 1


# -- override: an expiry is mandatory ----------------------------------------

def switches_path(tmp_path):
    return tmp_path / "switches.json"


def run_switches(tmp_path, *args, timeout=10):
    return run_control(
        tmp_path, *args,
        extra_env={"MONITORING_SWITCHES": str(switches_path(tmp_path))},
        timeout=timeout)


def test_override_with_neither_for_nor_until_is_refused(tmp_path):
    """The failure this guards against: a force set during a hunting trip
    and never cleared, which quietly flattens the battery."""
    result = run_switches(tmp_path, "override", "cameras", "on")
    assert result.returncode != 0
    assert "expiry" in result.stderr
    assert not switches_path(tmp_path).exists(), (
        "an override with no expiry was written anyway")


def test_override_with_both_for_and_until_is_refused(tmp_path):
    result = run_switches(tmp_path, "override", "cameras", "on",
                          "--for", "1h", "--until", "2026-10-18 09:00")
    assert result.returncode != 0
    assert not switches_path(tmp_path).exists()


def test_override_for_sets_an_expiry_roughly_that_far_out(tmp_path):
    before = time.time()
    result = run_switches(tmp_path, "override", "cameras", "on", "--for", "8h")
    assert result.returncode == 0, result.stderr
    with open(switches_path(tmp_path)) as handle:
        switches = json.load(handle)
    entry = switches["loads"]["cameras"]
    assert entry["force"] == "on"
    assert abs(entry["expires_epoch"] - (before + 8 * 3600)) < 30


def test_override_until_sets_the_given_time_in_the_sites_own_zone(tmp_path):
    """The zone has to come from location.yaml, the file the loop's own
    schedules are placed against, not from whatever zone this machine's
    clock happens to be set to -- a Pi left at the Debian default would
    otherwise set and report an expiry hours away from the one meant."""
    result = run_switches(tmp_path, "override", "cameras", "off",
                          "--until", "2026-10-18 09:00")
    assert result.returncode == 0, result.stderr
    expected = datetime.datetime(
        2026, 10, 18, 9, 0,
        tzinfo=zoneinfo.ZoneInfo("America/Chicago")).timestamp()
    with open(switches_path(tmp_path)) as handle:
        switches = json.load(handle)
    entry = switches["loads"]["cameras"]
    assert entry["force"] == "off"
    assert entry["expires_epoch"] == pytest.approx(expected)

    # And the line the operator reads back has to name the hour they typed,
    # not the same instant renamed in another zone.
    assert "2026-10-18 09:00" in result.stdout, result.stdout


def test_override_refuses_a_machine_with_no_location_file(tmp_path):
    """An expiry that cannot be placed against the site's own zone is not
    worth writing, so override fails the same way run, once and dry-run
    already do rather than falling back to a guess."""
    result = run_control(
        tmp_path, "override", "cameras", "off", "--until",
        "2026-10-18 09:00",
        extra_env={"MONITORING_LOCATION": str(tmp_path / "absent.yaml"),
                  "MONITORING_SWITCHES": str(switches_path(tmp_path))})
    assert result.returncode == 2
    assert "absent.yaml" in result.stderr
    assert "Traceback" not in result.stderr
    assert not switches_path(tmp_path).exists()


def test_override_clear_removes_a_prior_force(tmp_path):
    run_switches(tmp_path, "override", "cameras", "on", "--for", "1h")
    result = run_switches(tmp_path, "override", "clear", "cameras")
    assert result.returncode == 0, result.stderr
    with open(switches_path(tmp_path)) as handle:
        switches = json.load(handle)
    assert "cameras" not in switches["loads"]


def test_override_clear_without_a_load_name_is_refused(tmp_path):
    result = run_switches(tmp_path, "override", "clear")
    assert result.returncode != 0


def test_override_rejects_a_state_other_than_on_or_off(tmp_path):
    result = run_switches(tmp_path, "override", "cameras", "sideways",
                          "--for", "1h")
    assert result.returncode != 0


# -- shedding -----------------------------------------------------------------

def test_shedding_on_and_off_are_reported_by_status(tmp_path):
    result = run_switches(tmp_path, "shedding", "off")
    assert result.returncode == 0, result.stderr
    result = run_switches(tmp_path, "shedding", "status")
    assert "shedding: off" in result.stdout

    result = run_switches(tmp_path, "shedding", "on")
    assert result.returncode == 0, result.stderr
    result = run_switches(tmp_path, "shedding", "status")
    assert "shedding: on" in result.stdout


def test_shedding_status_reports_an_active_override(tmp_path):
    run_switches(tmp_path, "override", "cameras", "on", "--for", "8h")
    result = run_switches(tmp_path, "shedding", "status")
    assert result.returncode == 0, result.stderr
    assert "cameras" in result.stdout
    assert "forced on" in result.stdout


def test_a_dry_run_reads_the_switches_the_override_command_writes(tmp_path):
    """One file, written by one subcommand and read by the loop.

    A dry run exists to show what the controller would do, so it has to see
    the same overrides the controller would. Reading a different file means
    an operator sets an override, is told it is set, runs a dry run to check
    it, and is shown a decision taken as though they had set nothing.

    An override is the switch this can assert through the binary. Showing
    the shedding switch change a decision needs a tier latched whose
    schedule actually forbids the load, which here needs a trusted clock,
    and a trusted clock is a property of the machine rather than something a
    subprocess can be handed. That switch is pinned in the decision's own
    tests instead.
    """
    node = controlled_node(tmp_path)
    env = {"MONITORING_SWITCHES": str(switches_path(tmp_path))}

    set_result = run_control(tmp_path, "override", "cameras", "off",
                             "--for", "8h", extra_env=env)
    assert set_result.returncode == 0, set_result.stderr

    seen = run_control(tmp_path, "dry-run", "--node", str(node),
                       extra_env=env)
    assert seen.returncode == 0, seen.stderr
    assert "operator" in seen.stdout, seen.stdout


def test_the_status_listing_names_the_same_hour_the_override_was_set_for(
        tmp_path):
    """Setting an override and reading it back have to agree.

    Both name an hour, and each would otherwise pick its own zone: one
    answer against the site, one against whatever this machine's clock is
    set to. Two different answers for one override leave an operator with no
    way to tell which the loop will act on, and the number they would most
    reasonably trust is the one printed second.
    """
    set_result = run_switches(tmp_path, "override", "cameras", "on",
                              "--until", "2026-10-18 09:00")
    assert set_result.returncode == 0, set_result.stderr
    listed = run_switches(tmp_path, "shedding", "status")
    assert listed.returncode == 0, listed.stderr
    assert "2026-10-18 09:00" in listed.stdout, listed.stdout


def test_the_status_listing_works_without_a_location_file(tmp_path):
    """Whether shedding is on is the thing this command exists to answer, and
    it has to answer on a machine where no controller could start: a fresh
    card, before anybody has written the location file.

    It must not name an hour it cannot place, though, because an hour
    computed against the wrong zone is worse than an admission.
    """
    switches_path(tmp_path).write_text(json.dumps({
        "shedding_enabled": False,
        "loads": {"cameras": {"force": "on", "expires_epoch": 99999999999.0}}}))
    result = run_control(
        tmp_path, "shedding", "status",
        extra_env={"MONITORING_SWITCHES": str(switches_path(tmp_path)),
                   "MONITORING_LOCATION": str(tmp_path / "absent.yaml")})
    assert result.returncode == 0, result.stderr
    assert "shedding: off" in result.stdout
    assert "cannot read its location file" in result.stdout
    assert "2286" not in result.stdout, (
        "an hour was named against a zone this machine could not establish")


def test_shedding_status_with_no_override_says_so(tmp_path):
    result = run_switches(tmp_path, "shedding", "status")
    assert result.returncode == 0, result.stderr
    assert "none active" in result.stdout


def test_shedding_status_marks_an_expired_override_as_expired(tmp_path):
    switches_path(tmp_path).write_text(json.dumps({
        "shedding_enabled": True,
        "loads": {"cameras": {"force": "on", "expires_epoch": 1.0}}}))
    result = run_switches(tmp_path, "shedding", "status")
    assert result.returncode == 0, result.stderr
    assert "expired" in result.stdout


# -- white-box tests: no behaviour here has a black-box proxy ----------------
#
# Both properties below are real and both are required, but neither can be
# told apart from its absence by running the binary and watching its exit
# code or its output, for reasons specific to this environment and this
# design:
#
#   * a loop_interval default has nothing to read back from a process that
#     is killed after a fraction of a second, short of racing a 30 second
#     sleep in a test suite that otherwise runs in well under a minute;
#   * a relay that is released on shutdown looks identical, from outside,
#     to one that never was, on a machine with no GPIO hardware to leave
#     claimed in the first place -- the same reason test_relay.py reads
#     device.pin.state through gpiozero's mock factory rather than trusting
#     a double, except there is no mock factory for "was close() called".
#
# So these two go through the module directly, the same trade this project
# already makes in test_runner.py's own lock test when a race "has no
# reliable black-box proxy".

def _import_control_module():
    # spec_from_file_location cannot guess a loader from an extensionless
    # path, so the source loader is named explicitly.
    loader = importlib.machinery.SourceFileLoader("control_cli", str(CONTROL))
    spec = importlib.util.spec_from_loader("control_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def control_module():
    return _import_control_module()


class _Node:
    def __init__(self, control):
        self.path = "node.yaml"
        self.control = control


def test_the_loop_interval_defaults_to_thirty_seconds(control_module):
    assert control_module._loop_interval(_Node({})) == 30.0


def test_an_unreadable_loop_interval_is_a_config_error(control_module):
    with pytest.raises(control_module.config.ConfigError):
        control_module._loop_interval(_Node({"loop_interval": "sideways"}))


class _FakeMachine:
    def __init__(self, fail_after=None):
        self.once_calls = 0
        self.closed = False
        self.fail_after = fail_after

    def once(self):
        self.once_calls += 1
        if self.fail_after is not None and self.once_calls >= self.fail_after:
            raise RuntimeError("a relay this machine could not drive")

    def close(self):
        self.closed = True


def test_run_closes_the_relays_on_a_clean_stop(control_module):
    machine = _FakeMachine()
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 2

    control_module._run(machine, 0, should_stop, sleep=lambda s: None)
    assert machine.closed, "the relays were not released on a clean stop"
    assert machine.once_calls >= 1


def test_run_closes_the_relays_even_when_a_pass_raises(control_module):
    """The fail-safe depends on a dying process releasing its pins, which
    has to hold whether the process is dying because it was asked to stop
    or because a pass itself failed."""
    machine = _FakeMachine(fail_after=1)
    with pytest.raises(RuntimeError):
        control_module._run(machine, 0, lambda: False, sleep=lambda s: None)
    assert machine.closed
