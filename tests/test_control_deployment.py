"""The load controller's systemd unit and tmpfiles entry must keep the
promises the loop and the relay module make in code.

monitoring/control/loop.py refuses to run before a boot identity is known,
and monitoring/control/relay.py's whole fail-safe story rests on a dying
process releasing its GPIO pin. Neither of those is worth anything if the
unit that starts the loop runs as the wrong user, restarts in a tight loop,
or lets the lgpio backend fall back to one that does not release the pin.
This is the deployment side of that story, checked against the real files
rather than against a description of them.
"""

import os
import pathlib
import re

from monitoring.control import files as control_files

REPO = pathlib.Path(__file__).resolve().parent.parent

UNIT_PATH = REPO / "deploy" / "monitoring-control.service"
TMPFILES_PATH = REPO / "deploy" / "tmpfiles-monitoring.conf"

UNIT_RAW = UNIT_PATH.read_text()
TMPFILES_RAW = TMPFILES_PATH.read_text()


#: Both deployment files use "#" for comments, the same as the shell script
#: tests/test_deployment.py already checks. A protection that has been
#: commented out -- RuntimeDirectory=, say, mentioned only in the prose
#: explaining why it is not used -- still appears in the raw text, so every
#: assertion below reads the stripped version instead. A line carrying a
#: quote is kept whole rather than stripped, the same exception
#: tests/test_deployment.py makes, since a hash inside a quoted value is not
#: a comment; none of the directive lines here use quotes, so the exception
#: never shields one of them from being checked.
def _strip_comments(text):
    out = []
    for line in text.splitlines():
        if "'" in line or '"' in line:
            out.append(line)
            continue
        line = line.split("#", 1)[0]
        if line.strip():
            out.append(line)
    return "\n".join(out)


UNIT = _strip_comments(UNIT_RAW)
TMPFILES = _strip_comments(TMPFILES_RAW)


def _value(directive, text):
    """The value of the first `directive=...` line, or None."""
    match = re.search(r"(?m)^%s\s*=\s*(.*)$" % re.escape(directive), text)
    return match.group(1).strip() if match else None


def test_the_unit_runs_the_installed_path_not_a_checkout():
    """A path under a developer's checkout works on one laptop and nowhere
    the service is actually deployed."""
    exec_start = _value("ExecStart", UNIT)
    assert exec_start, "no ExecStart= line"
    assert exec_start.startswith("/opt/monitoring/bin/control"), exec_start
    assert "/etc/monitoring/node.yaml" in exec_start, exec_start
    assert str(REPO) not in UNIT, (
        "the unit names this checkout's own path, which will not exist on "
        "the machine it is installed to")


def test_the_unit_does_not_use_runtime_directory():
    """RuntimeDirectory= deletes /run/monitoring when this unit stops,
    restarts or fails, which would also delete /run/monitoring/readings --
    the collectors' cache, written by a process this unit does not own.
    The tmpfiles entry creates the directory instead, for the life of the
    boot rather than the life of this unit."""
    assert "RuntimeDirectory" not in UNIT, (
        "RuntimeDirectory= looks like the obvious way to create "
        "/run/monitoring, and it is wrong here: stopping or restarting this "
        "unit would delete the directory and the collectors' reading cache "
        "with it. Let deploy/tmpfiles-monitoring.conf create it instead.")


def test_the_unit_names_the_lgpio_backend():
    """The sysfs backend does not release a pin when the process holding it
    dies, so every restart would find the pin busy and the controller would
    never recover. This is belt and braces with what relay.py already sets,
    so a future change to its import order cannot silently fall through to
    that backend."""
    assert "GPIOZERO_PIN_FACTORY=lgpio" in UNIT


def test_the_unit_restarts_when_it_stops():
    """Every load's fail-safe is only safe for as long as nobody is supposed
    to be driving it. A supervisor putting the controller back is better
    than leaving a load shed, or connected, because the process that was
    meant to be deciding is gone."""
    assert _value("Restart", UNIT) == "always"


def test_the_unit_waits_before_restarting():
    """Without a delay, a failing controller restarts in a tight loop, and a
    load wired to its normally closed contact twitches on every cycle --
    once a second is its own kind of damage to a recorder or a pump."""
    value = _value("RestartSec", UNIT)
    assert value is not None, "no RestartSec=, so a crash restarts instantly"
    assert int(value) > 0


def test_the_unit_does_not_run_as_root():
    """The controller only ever needs what the telegraf user already has:
    the gpio group and the lgpio pin factory install.sh sets up for it.
    Running as root would mean a second, unnecessary user with the run of
    the whole machine instead of just /run/monitoring and
    /var/lib/monitoring."""
    user = _value("User", UNIT)
    assert user, "no User= line, so systemd would run this as root"
    assert user != "root"


def test_the_tmpfiles_entry_creates_what_the_code_actually_writes_under_run():
    """Checked against the constants in monitoring/control/files.py, not
    against the literal paths, so a path moved in that module fails here
    instead of silently leaving an unwritable directory on the next
    deploy."""
    covered = set()
    for line in TMPFILES.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("d", "D"):
            covered.add(parts[1])

    needed = {
        control_files.RUNTIME_DIR,
        control_files.READINGS_DIR,
        os.path.dirname(control_files.CONTROL_STATE),
        os.path.dirname(control_files.PUBLISHED),
    }
    missing = needed - covered
    assert not missing, (
        "tmpfiles-monitoring.conf does not create %s, which "
        "monitoring/control/files.py writes under /run" % sorted(missing))


def test_the_tmpfiles_entry_owns_the_directories_as_telegraf():
    """Whoever creates /run/monitoring first decides who owns it. If that is
    a hand run as root, the service can never write it again until the next
    reboot clears /run. Owning it here, as the same user both the collectors
    and the controller run as, means the first write is never the thing
    deciding."""
    for line in TMPFILES.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("d", "D") and \
                parts[1] in (control_files.RUNTIME_DIR, control_files.READINGS_DIR):
            assert len(parts) >= 4, line
            assert parts[3] == "telegraf", (
                "%s is not owned by telegraf, so the service that runs as "
                "telegraf cannot write it" % parts[1])


def test_the_unit_starts_after_the_directories_are_made():
    """The controller cannot create /run/monitoring itself: the telegraf user
    may write inside a directory granted to it but not create one directly
    under /run."""
    assert "systemd-tmpfiles-setup.service" in _value("After", UNIT)


def test_the_unit_does_not_decline_to_start_on_a_condition():
    """A failed condition is silent. systemd skips the unit, logs one line,
    and leaves it inactive with no error and no restart, because Restart=
    does not apply to a unit that never started.

    So a missing directory would give a controller that is quietly not
    running, with every load at its fail-safe and nothing managing the
    battery. ReadWritePaths= already requires the directory to exist, which
    fails the unit loudly and lets the restart retry it, so a condition buys
    nothing and costs the only signal anybody would see.
    """
    for directive in UNIT.splitlines():
        assert not directive.strip().startswith("Condition"), (
            "%s makes a missing path a silent skip rather than a visible "
            "failure" % directive.strip())


def test_the_service_can_still_reach_an_install_under_a_home_directory():
    """The installer symlinks /opt/monitoring at wherever it was run from,
    and a copy in a home directory is a documented way to install this.

    ProtectHome=true replaces /home with an empty directory for the service,
    so that layout becomes a unit that cannot start at all: the executable
    named in ExecStart is not there. The failure is especially unhelpful
    because the path exists and is readable to anybody who checks by hand.
    read-only keeps the part of the setting that matters, since this service
    has no business writing to a home directory and its own writable state
    is listed explicitly elsewhere.
    """
    assert _value("ProtectHome", UNIT) == "read-only", (
        "ProtectHome must not hide /home from a service whose executable "
        "can be reached through it")
    assert "ExecStart=/opt/monitoring/" in UNIT, (
        "this test is pinned to the path the installer links; if ExecStart "
        "moved, check whether it can still be reached through a symlink")


def test_the_installer_really_does_link_opt_monitoring_anywhere():
    """The other half of the test above. It asserts a hardening setting is
    loosened for a layout the installer creates, so it is only worth
    anything while the installer still creates it."""
    install = (REPO / "deploy" / "install.sh").read_text()
    assert "ln -sfn" in install and "/opt/monitoring" in install, (
        "nothing links /opt/monitoring any more, so ProtectHome could be "
        "tightened again")


def test_the_writable_paths_cover_everything_the_code_writes():
    """With ProtectSystem=strict the rest of the filesystem is read-only, so
    a path the code writes and this line omits fails at run time rather than
    at deploy.

    Read from the constants rather than the literals, so moving one fails
    here. The one most easily lost is the persistent directory: without it an
    override cannot be written and the clock's high-water mark cannot
    advance, which is the bound that catches a clock that has been reset.
    """
    allowed = set(_value("ReadWritePaths", UNIT).split())
    for path in (control_files.RUNTIME_DIR, control_files.PERSIST_DIR):
        assert path in allowed, (
            "%s is not writable, but monitoring/control/files.py writes "
            "there" % path)
    for path in (control_files.CONTROL_STATE, control_files.PUBLISHED,
                 control_files.SWITCHES, control_files.HIGH_WATER):
        assert any(path.startswith(root + "/") for root in allowed), (
            "%s is not under any writable path" % path)
