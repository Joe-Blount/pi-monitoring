"""Put a factory-reset Wavlink back the way it was, from a Pi that can hear it.

The access point at the far end of a long wireless link can lose its settings
when its power fails. It then broadcasts its factory network with no uplink, so
everything behind it is off the network and nothing can reach it except a radio
in range. A Pi in range can do the repair: join the factory network, log in,
upload a saved copy of the settings, and go back to the normal network.

The trigger is deliberately narrow. A lost link, a dead router or a Wavlink that
is merely rebooting never causes a repair, because a repair needs positive
evidence of a reset: the normal network absent and the factory network present,
on the radio address of this one unit, at two checks in a row. A different unit
in range has a different address and is left alone.

Attempts are limited, and each is recorded before it starts, so a repair that
hangs or dies still counts against the limit.
"""

import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse

import yaml

CONFIG_PATH = "/etc/monitoring/wavlink-recover.yaml"
STATE_PATH = "/var/lib/wavlink-recover/state.json"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

REQUIRED = ("interface", "factory_ssid", "factory_bssid", "normal_ssid",
            "normal_profile", "admin_password", "settings_file", "gateway")
DEFAULTS = {
    "factory_ip": "192.168.10.1",
    "pi_address": "192.168.10.53",
    "factory_password": "admin",
    "web_name": "wifi.wavlink.com",
    "time_zone": "UTC+06:00",
    "region": "US",
    "sightings_needed": 2,
    "max_attempts_per_day": 3,
    "minutes_between_attempts": 30,
    "seconds_to_return": 300,
}
TEMP_PROFILE = "wavlink-recover"
DAY = 24 * 60 * 60

# curl exit codes that mean the unit stopped answering part way through the
# upload. It restarts to apply the file and may never reply, so these are not
# failures. A refused or unreachable connection is a different code.
UPLOAD_CUT_OFF = {18, 28, 52, 56}


class RepairFailed(Exception):
    """A repair step did not work. The message names the step."""


class ConfigError(Exception):
    """The configuration or the saved settings cannot be used."""


def normalize_bssid(value):
    return value.strip().replace("\\", "").lower()


def load_config(path=CONFIG_PATH):
    try:
        with open(path) as f:
            loaded = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e))
    if not isinstance(loaded, dict):
        raise ConfigError("%s is not a mapping" % path)
    missing = [k for k in REQUIRED if not loaded.get(k)]
    if missing:
        raise ConfigError("%s is missing %s" % (path, ", ".join(missing)))
    config = dict(DEFAULTS)
    config.update(loaded)
    config["factory_bssid"] = normalize_bssid(config["factory_bssid"])
    return config


def check_settings(config):
    """Refuse a settings file that would not restore this unit's job."""
    try:
        with open(config["settings_file"], "rb") as f:
            lines = f.read().decode("latin-1").splitlines()
    except OSError as e:
        raise ConfigError("cannot read the settings file: %s" % e)
    wanted = {
        "SSID1": config["normal_ssid"],
        "OperationMode": "3",
        "ApCliEnable": "1",
    }
    found = dict(line.split("=", 1) for line in lines if "=" in line)
    for key, value in wanted.items():
        if found.get(key) != value:
            raise ConfigError(
                "the settings file has %s=%r, expected %r"
                % (key, found.get(key), value))


# -- state -------------------------------------------------------------------

def load_state(path=STATE_PATH):
    """Read the attempt record. A record that exists and cannot be read raises:
    guessing an empty one would hand back every attempt the limit has used."""
    try:
        with open(path) as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"sightings": 0, "attempts": []}
    if (not isinstance(state, dict)
            or not isinstance(state.get("sightings"), int)
            or not isinstance(state.get("attempts"), list)):
        raise ValueError("unexpected contents in %s" % path)
    return state


def current_boot():
    with open(BOOT_ID_PATH) as f:
        return f.read().strip()


def save_state(state, path=STATE_PATH):
    # Readable by everyone, so the collector can report the attempts. The
    # record holds times and results and nothing secret.
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    os.chmod(directory, 0o755)
    temp = "%s.tmp" % path
    with open(temp, "w") as f:
        json.dump(state, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    # The rename is not durable until the directory is. Without this, a power
    # cut can hand back attempts the limit has already used.
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# -- deciding ----------------------------------------------------------------

def parse_scan(text):
    """Turn terse nmcli output into (ssid, bssid) pairs. nmcli escapes each
    colon inside a field with a backslash."""
    pairs = []
    for line in text.splitlines():
        fields = []
        current = ""
        escaped = False
        for char in line:
            if escaped:
                current += char
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == ":":
                fields.append(current)
                current = ""
            else:
                current += char
        fields.append(current)
        if len(fields) == 2:
            pairs.append((fields[0], normalize_bssid(fields[1])))
    return pairs


def is_factory_state(pairs, config):
    ssids = {ssid for ssid, _ in pairs}
    if config["normal_ssid"] in ssids:
        return False
    return (config["factory_ssid"], config["factory_bssid"]) in pairs


def attempts_in_last_day(state, now):
    """Attempts still counting against the limit. A Pi with no network and no
    battery clock can boot with a time behind the one it recorded. An attempt
    stamped ahead of the clock is moved back to the clock, so it still counts
    and still expires: left as it was, it would hold off every repair until
    the clock caught up, and a broken network is what keeps the clock behind."""
    for attempt in state["attempts"]:
        attempt["at"] = min(attempt["at"], now)
    return [a for a in state["attempts"] if now - a["at"] < DAY]


def correct_times(state, boot, now, monotonic):
    """Move each attempt's time to the right one, once the clock is right.

    The Pi has no battery clock, and a broken network is what keeps it from
    setting the time, so a repair usually happens with the clock wrong. Each
    attempt keeps the boot and the monotonic time it happened at. Later in
    the same boot, on a clock that is right, the attempt's time is the time
    now less the monotonic time since.

    Call it only on a clock that is right. Returns whether anything moved.
    """
    moved = False
    for attempt in state["attempts"]:
        stamp = attempt.get("monotonic")
        if (attempt.get("clock_trusted") is False
                and attempt.get("boot") == boot and boot is not None
                and isinstance(stamp, (int, float))
                and not isinstance(stamp, bool)):
            attempt["at"] = now - max(0.0, monotonic - stamp)
            attempt["clock_trusted"] = True
            moved = True
    return moved


def decide(pairs, state, config, now, boot=None):
    """Say what this check should do. Updates the sighting count in state.
    Returns (action, reason); action is idle, wait, limited or repair.

    Sightings only add up within one boot. The unit and the Pi share a battery,
    so a sighting from before a power cut says nothing about the unit as it
    starts up."""
    if state.get("boot") != boot:
        state["sightings"] = 0
        state["boot"] = boot
    if not is_factory_state(pairs, config):
        state["sightings"] = 0
        return "idle", "no factory-reset unit in range"
    state["sightings"] += 1
    if state["sightings"] < config["sightings_needed"]:
        return "wait", "sighting %d of %d" % (
            state["sightings"], config["sightings_needed"])
    recent = attempts_in_last_day(state, now)
    if len(recent) >= config["max_attempts_per_day"]:
        return "limited", "%d attempts in the last day" % len(recent)
    if recent:
        since = now - max(a["at"] for a in recent)
        gap = config["minutes_between_attempts"] * 60
        if since < gap:
            return "limited", "last attempt was too recent"
    return "repair", "factory-reset unit in range"


# -- talking to the machine --------------------------------------------------

def run_command(args, input_text=None, timeout=60):
    """Run a command and return (exit code, standard output). A command that
    outlasts its timeout reports curl's own code for that, 28."""
    try:
        done = subprocess.run(args, input=input_text, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 28, ""
    except FileNotFoundError:
        return 127, ""
    return done.returncode, done.stdout


def scan(config, run=run_command):
    code, out = run(["nmcli", "-t", "-f", "SSID,BSSID", "device", "wifi",
                     "list", "--rescan", "yes", "ifname", config["interface"]])
    if code != 0:
        raise RepairFailed("wifi scan exited %d" % code)
    return parse_scan(out)


def join_factory(config, run):
    run(["nmcli", "connection", "delete", TEMP_PROFILE])
    code, _ = run([
        "nmcli", "connection", "add", "type", "wifi",
        "ifname", config["interface"], "con-name", TEMP_PROFILE,
        "ssid", config["factory_ssid"],
        "802-11-wireless.bssid", config["factory_bssid"],
        "connection.autoconnect", "no",
        "ipv4.method", "manual",
        "ipv4.addresses", config["pi_address"] + "/24",
        "ipv4.never-default", "yes",
        "ipv6.method", "disabled"])
    if code != 0:
        raise RepairFailed("could not create the temporary profile")
    code, _ = run(["nmcli", "--wait", "45", "connection", "up", TEMP_PROFILE],
                  timeout=60)
    if code != 0:
        raise RepairFailed("could not join the factory network")


def remove_leftover_profile(run):
    """A check killed outright cannot clean up after itself, and the temporary
    profile would hold the radio on the factory network. Removing an absent
    profile is a harmless error."""
    run(["nmcli", "connection", "delete", TEMP_PROFILE])


def leave_factory(run):
    run(["nmcli", "connection", "down", TEMP_PROFILE])
    run(["nmcli", "connection", "delete", TEMP_PROFILE])


class WebSession:
    """The unit's web interface. Every page redirects to one host name, which
    only the unit's own DNS answers, so curl is told where that name lives."""

    def __init__(self, config, directory, run):
        self.config = config
        self.jar = os.path.join(directory, "cookies")
        self.directory = directory
        self.run = run
        self.host = config["web_name"]
        self.base = "http://" + self.host
        self.setup_page = "sysinit.shtml"

    def _curl(self, seconds, referer):
        return ["curl", "-s", "-m", str(seconds),
                "-A", "Mozilla/5.0",
                "--resolve", "%s:80:%s" % (self.host, self.config["factory_ip"]),
                "-c", self.jar, "-b", self.jar,
                "-H", "Referer: %s/%s" % (self.base, referer),
                "-H", "Origin: " + self.base]

    def login(self, password):
        """Returns main or sysinit, naming the page the unit sends a good login
        to, or None when it refuses."""
        key = "M%d" % random.randrange(10**7, 10**8)
        digest = hashlib.md5((key + password).encode()).hexdigest()
        body = ("newUI=1&page=login&username=admin&langChange=0&password=%s"
                "&ipaddr=%s&homepage=main.shtml&login_page=login.shtml%%3F%s"
                "&hostname=%s&key=%s&lang_select=0"
                % (digest, self.config["pi_address"], key, self.host, key))
        code, out = self.run(
            self._curl(15, "") + ["--data", "@-",
                                  self.base + "/cgi-bin/login.cgi"],
            input_text=body, timeout=20)
        if code != 0:
            raise RepairFailed("login request exited %d" % code)
        target = re.search(r'URL=([^"\s>]*)', out)
        if target is None:
            return None
        if "sysinit" in target.group(1):
            self.setup_page = target.group(1).rsplit("/", 1)[-1]
            return "sysinit"
        if "main.shtml" in target.group(1):
            return "main"
        return None

    def first_time_setup(self, password):
        zone = self.config["time_zone"].replace("+", "%2B").replace(":", "%3A")
        body = ("page=sysinit&lang_select=0&wl_reddomain=%s&time_zone=%s"
                "&newpass=%s" % (self.config["region"], zone,
                                  urllib.parse.quote(password, safe="")))
        code, _ = self.run(
            self._curl(20, self.setup_page) + [
                "--data", "@-", self.base + "/cgi-bin/login.cgi"],
            input_text=body, timeout=25)
        if code != 0:
            raise RepairFailed("first-time setup request exited %d" % code)

    def upload(self, settings_file):
        # The name is the one the unit's own export gives the file.
        copy = os.path.join(self.directory, "RT2880_Settings.dat")
        shutil.copyfile(settings_file, copy)
        code, out = self.run(
            self._curl(40, "reset.shtml") + [
                "-o", os.devnull, "-w", "%{http_code}",
                "-F", "files=@%s;type=application/octet-stream" % copy,
                self.base + "/cgi-bin/upload_settings.cgi"],
            timeout=50)
        if code in UPLOAD_CUT_OFF:
            return
        if code != 0:
            raise RepairFailed("upload request exited %d" % code)
        if out.strip() not in ("000", "200", "302"):
            raise RepairFailed("upload answered HTTP %s" % out.strip())


def log_in_to_unit(session, config, sleep):
    """Reach a logged-in session from whichever state the unit is in. A unit an
    earlier attempt got part way through already has the set password."""
    page = session.login(config["factory_password"])
    if page == "sysinit":
        session.first_time_setup(config["admin_password"])
        sleep(13)
        page = session.login(config["admin_password"])
    elif page is None:
        page = session.login(config["admin_password"])
    if page is None:
        raise RepairFailed("the unit refused both passwords")
    if page == "sysinit":
        raise RepairFailed("the unit stayed in first-time setup")


def wait_for_return(config, run, sleep, clock):
    """After the upload, wait for the normal network, join it, and prove the
    uplink by reaching the router through it. Joining by name needs no scan
    first, and a scan NetworkManager is holding off would only cost time."""
    limit = config["seconds_to_return"]
    deadline = clock() + limit
    while clock() < deadline:
        sleep(20)
        code, _ = run(["nmcli", "--wait", "45", "connection", "up",
                       config["normal_profile"]], timeout=60)
        if code != 0:
            continue
        code, _ = run(["ping", "-c", "3", "-W", "3", "-I", config["interface"],
                       config["gateway"]], timeout=20)
        if code == 0:
            return
    raise RepairFailed(
        "the normal network and the router were not both back in time")


def repair(config, run=run_command, sleep=time.sleep, clock=time.monotonic):
    """Run one repair. Raises RepairFailed naming the step that failed. The
    temporary profile is removed however it ends, which hands the radio back
    to NetworkManager's saved networks."""
    with tempfile.TemporaryDirectory(prefix="wavlink-recover-") as directory:
        os.chmod(directory, 0o700)
        try:
            join_factory(config, run)
            session = WebSession(config, directory, run)
            log_in_to_unit(session, config, sleep)
            session.upload(config["settings_file"])
            # Stay on the unit's network while it takes the file in. It carried
            # on working for about a minute after the request ended.
            sleep(30)
        finally:
            leave_factory(run)
    wait_for_return(config, run, sleep, clock)


def check(config, state_path=STATE_PATH, dry_run=False, run=run_command,
          sleep=time.sleep, now=time.time, clock=time.monotonic, say=print,
          boot=None, synced=None):
    """One timed check. Returns the process exit code.

    `synced` says whether the wall clock is right. It defaults to the same
    test the load controller uses."""
    check_settings(config)
    if synced is None:
        from monitoring.control import clock as wall_clock
        synced = lambda: wall_clock.assess().trusted  # noqa: E731
    state = load_state(state_path)
    before = json.dumps(state, sort_keys=True)
    trusted = bool(synced())
    if trusted:
        correct_times(state, boot, now(), clock())
    if not dry_run:
        remove_leftover_profile(run)
    pairs = scan(config, run)
    action, reason = decide(pairs, state, config, now(), boot)
    say("%s: %s" % (action, reason))
    if dry_run:
        return 0
    if action != "repair":
        # Unchanged state is not rewritten: this runs every five minutes, for
        # years, on an SD card.
        if json.dumps(state, sort_keys=True) != before:
            save_state(state, state_path)
        return 0
    # The boot and the monotonic time, so a time taken on a wrong clock can
    # be corrected later in this boot.
    attempt = {"at": now(), "result": "started", "boot": boot,
               "monotonic": clock(), "clock_trusted": trusted}
    state["attempts"].append(attempt)
    save_state(state, state_path)
    try:
        repair(config, run, sleep, clock)
    except (RepairFailed, OSError) as e:
        attempt["result"] = "failed: %s" % e
        say(attempt["result"])
        save_state(state, state_path)
        return 1
    attempt["result"] = "restored"
    state["sightings"] = 0
    say("restored")
    save_state(state, state_path)
    return 0


class Lock:
    """Keep a hand run and the timer from repairing at the same time."""

    def __init__(self, state_path=STATE_PATH):
        self.path = state_path + ".lock"
        self.file = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.file = open(self.path, "w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RepairFailed("another check is running")
        return self

    def __exit__(self, *exc):
        self.file.close()
