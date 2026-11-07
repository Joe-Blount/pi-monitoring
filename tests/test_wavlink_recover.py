"""A repair takes a site's access point through its factory state, so what
matters most is when it refuses to act: the wrong unit, too little evidence,
too many attempts, or a settings file that would not restore the unit's job."""

import hashlib
import json
import os
import pathlib
import runpy
import sys

import pytest

from monitoring import wavlink_recover as wr

REPO = pathlib.Path(__file__).resolve().parent.parent
THE_UNIT = "02:00:00:00:00:01"
OTHER_UNIT = "02:00:00:00:00:02"
NOW = 1_000_000.0
HOUR = 3600


@pytest.fixture
def config(tmp_path):
    settings = tmp_path / "settings.cfg"
    settings.write_text("OperationMode=3\nSSID1=normal\nApCliEnable=1\n")
    return dict(
        wr.DEFAULTS,
        interface="wlan0",
        factory_ssid="WAVLINK-N",
        factory_bssid=THE_UNIT,
        normal_ssid="normal",
        normal_profile="normal-profile",
        admin_password="set password",
        settings_file=str(settings),
        gateway="192.168.50.1",
    )


def fresh_state():
    return {"sightings": 0, "attempts": []}


RESET = [("WAVLINK-N", THE_UNIT), ("neighbor", "aa:bb:cc:dd:ee:ff")]


# -- reading a scan ----------------------------------------------------------

def test_a_scan_line_splits_on_the_colons_nmcli_does_not_escape():
    text = "WAVLINK-N:02\\:00\\:00\\:00\\:00\\:01\n"
    assert wr.parse_scan(text) == [("WAVLINK-N", THE_UNIT)]


def test_a_colon_inside_a_network_name_stays_in_the_name():
    text = "a\\:b:02\\:00\\:00\\:00\\:00\\:01\n"
    assert wr.parse_scan(text) == [("a:b", THE_UNIT)]


def test_a_hidden_network_has_an_empty_name():
    assert wr.parse_scan(":02\\:00\\:00\\:00\\:00\\:01\n") == [("", THE_UNIT)]


# -- what counts as evidence -------------------------------------------------

def test_a_lost_link_with_no_factory_network_is_not_a_reset(config):
    action, _ = wr.decide([("neighbor", OTHER_UNIT)], fresh_state(), config, NOW)
    assert action == "idle"


def test_the_factory_network_from_a_different_unit_is_not_a_reset(config):
    state = {"sightings": 5, "attempts": []}
    action, _ = wr.decide([("WAVLINK-N", OTHER_UNIT)], state, config, NOW)
    assert action == "idle"


def test_the_normal_network_in_range_means_no_repair(config):
    pairs = RESET + [("normal", THE_UNIT)]
    state = {"sightings": 5, "attempts": []}
    action, _ = wr.decide(pairs, state, config, NOW)
    assert action == "idle"


def test_one_sighting_is_not_enough(config):
    action, _ = wr.decide(RESET, fresh_state(), config, NOW)
    assert action == "wait"


def test_two_sightings_in_a_row_start_a_repair(config):
    state = fresh_state()
    wr.decide(RESET, state, config, NOW)
    action, _ = wr.decide(RESET, state, config, NOW)
    assert action == "repair"


def test_a_clean_check_clears_the_sightings(config):
    state = fresh_state()
    wr.decide(RESET, state, config, NOW)
    wr.decide([], state, config, NOW)
    action, _ = wr.decide(RESET, state, config, NOW)
    assert action == "wait"


def test_the_address_matches_whatever_case_the_config_uses(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "interface: wlan0\nfactory_ssid: W\nfactory_bssid: '02:00:00:00:00:01'\n"
        "normal_ssid: n\nnormal_profile: n\nadmin_password: p\n"
        "settings_file: s\ngateway: g\n")
    assert wr.load_config(str(path))["factory_bssid"] == THE_UNIT


def test_a_config_missing_a_field_is_refused(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("interface: wlan0\n")
    with pytest.raises(wr.ConfigError, match="factory_bssid"):
        wr.load_config(str(path))


# -- the limit ---------------------------------------------------------------

def seen(attempts):
    return {"sightings": 1, "attempts": [{"at": a, "result": "x"} for a in attempts]}


def test_three_attempts_in_a_day_stop_further_repairs(config):
    state = seen([NOW - 3 * HOUR, NOW - 2 * HOUR, NOW - HOUR])
    action, _ = wr.decide(RESET, state, config, NOW)
    assert action == "limited"


def test_attempts_older_than_a_day_no_longer_count(config):
    state = seen([NOW - 26 * HOUR, NOW - 25 * HOUR, NOW - 24 * HOUR - 1])
    action, _ = wr.decide(RESET, state, config, NOW)
    assert action == "repair"


def test_attempts_are_spaced_apart(config):
    action, _ = wr.decide(RESET, seen([NOW - 600]), config, NOW)
    assert action == "limited"


def test_an_attempt_after_the_gap_is_allowed(config):
    action, _ = wr.decide(RESET, seen([NOW - HOUR]), config, NOW)
    assert action == "repair"


def test_a_clock_that_went_backwards_does_not_refill_the_allowance(config):
    state = seen([NOW + HOUR, NOW + 2 * HOUR, NOW + 3 * HOUR])
    action, _ = wr.decide(RESET, state, config, NOW)
    assert action == "limited"


def test_a_clock_days_behind_does_not_hold_off_repairs_for_days(config):
    state = seen([NOW + 7 * 24 * HOUR])
    action, _ = wr.decide(RESET, state, config, NOW)
    assert action == "limited"
    action, _ = wr.decide(RESET, state, config, NOW + HOUR)
    assert action == "repair"


def test_a_sighting_from_before_a_reboot_does_not_count(config):
    state = {"sightings": 1, "attempts": [], "boot": "earlier"}
    action, _ = wr.decide(RESET, state, config, NOW, boot="later")
    assert action == "wait"
    action, _ = wr.decide(RESET, state, config, NOW, boot="later")
    assert action == "repair"


# -- the record --------------------------------------------------------------

def test_a_missing_record_starts_empty(tmp_path):
    assert wr.load_state(str(tmp_path / "none.json")) == fresh_state()


def test_an_unreadable_record_is_not_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not json")
    with pytest.raises(ValueError):
        wr.load_state(str(path))


# -- the settings file -------------------------------------------------------

@pytest.mark.parametrize("contents", [
    "OperationMode=0\nSSID1=normal\nApCliEnable=1\n",
    "OperationMode=3\nSSID1=WAVLINK-N\nApCliEnable=1\n",
    "OperationMode=3\nSSID1=normal\nApCliEnable=0\n",
    "",
])
def test_a_settings_file_that_would_not_restore_the_unit_is_refused(config, contents):
    pathlib.Path(config["settings_file"]).write_text(contents)
    with pytest.raises(wr.ConfigError):
        wr.check_settings(config)


def test_a_missing_settings_file_is_refused(config):
    config["settings_file"] = "/nonexistent/settings.cfg"
    with pytest.raises(wr.ConfigError):
        wr.check_settings(config)


# -- a whole repair, against a scripted machine ------------------------------

class Machine:
    """Answers the commands a repair runs, and records them."""

    def __init__(self, scans=None, login_pages=None, upload=(0, "200"),
                 join_code=0, ping_code=0):
        self.commands = []
        self.scans = list(scans or [])
        self.login_pages = list(login_pages or ["sysinit", "main"])
        self.upload = upload
        self.join_code = join_code
        self.ping_code = ping_code
        self.inputs = []

    def __call__(self, args, input_text=None, timeout=60):
        self.commands.append(args)
        self.inputs.append(input_text)
        joined = " ".join(args)
        if args[0] == "nmcli" and "wifi" in args:
            return 0, self.scans.pop(0) if self.scans else "normal:AA\\:BB\n"
        if args[0] == "nmcli" and "up" in args and wr.TEMP_PROFILE in args:
            return self.join_code, ""
        if args[0] == "ping":
            return self.ping_code, ""
        if "upload_settings" in joined:
            return self.upload
        if "login.cgi" in joined and input_text.startswith("page=sysinit"):
            return 0, ""
        if "login.cgi" in joined:
            page = self.login_pages.pop(0)
            if page is None:
                return 0, '<meta content="0; URL=login.shtml?login=1">'
            return 0, '<meta content="0; URL=http://x/%s.shtml?M1">' % page
        return 0, ""

    def ran(self, text):
        return any(text in " ".join(c) for c in self.commands)

    def order_of(self, text):
        return next(i for i, c in enumerate(self.commands) if text in " ".join(c))


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, seconds):
        self.t += seconds

    def __call__(self):
        return self.t


def do_repair(config, machine):
    clock = Clock()
    wr.repair(config, machine, clock.sleep, clock)


def test_a_repair_uploads_after_first_time_setup_and_comes_home(config):
    machine = Machine()
    do_repair(config, machine)
    assert machine.order_of("con-name") < machine.order_of("upload_settings")
    assert machine.order_of("upload_settings") < machine.order_of("normal-profile")
    assert any(i and i.startswith("page=sysinit") for i in machine.inputs)
    assert machine.ran("ping")


def test_the_temporary_profile_only_ever_joins_the_one_unit(config):
    machine = Machine()
    do_repair(config, machine)
    add = next(c for c in machine.commands if "con-name" in c)
    assert add[add.index("802-11-wireless.bssid") + 1] == THE_UNIT
    assert add[add.index("connection.autoconnect") + 1] == "no"
    assert add[add.index("ipv4.never-default") + 1] == "yes"


def test_the_password_is_not_on_any_command_line(config):
    machine = Machine()
    do_repair(config, machine)
    assert not any("set password" in " ".join(c) or "set%20password" in " ".join(c)
                   for c in machine.commands)
    assert any(i and "newpass=set%20password" in i for i in machine.inputs)


def test_a_unit_part_way_through_a_repair_takes_the_set_password(config):
    machine = Machine(login_pages=[None, "main"])
    do_repair(config, machine)
    assert machine.ran("upload_settings")
    assert not any(i and i.startswith("page=sysinit") for i in machine.inputs)


def test_a_refused_login_uploads_nothing(config):
    machine = Machine(login_pages=[None, None])
    with pytest.raises(wr.RepairFailed, match="refused"):
        do_repair(config, machine)
    assert not machine.ran("upload_settings")


def test_a_failed_repair_still_removes_the_temporary_profile(config):
    machine = Machine(login_pages=[None, None])
    with pytest.raises(wr.RepairFailed):
        do_repair(config, machine)
    assert machine.commands[-1][:3] == ["nmcli", "connection", "delete"]


def test_failing_to_join_uploads_nothing(config):
    machine = Machine(join_code=4)
    with pytest.raises(wr.RepairFailed, match="join"):
        do_repair(config, machine)
    assert not machine.ran("upload_settings")


@pytest.mark.parametrize("code", sorted(wr.UPLOAD_CUT_OFF))
def test_a_unit_that_stops_answering_during_the_upload_is_not_a_failure(config, code):
    do_repair(config, Machine(upload=(code, "000")))


def test_an_upload_the_unit_rejects_is_a_failure(config):
    with pytest.raises(wr.RepairFailed, match="500"):
        do_repair(config, Machine(upload=(0, "500")))


def test_an_unreachable_unit_at_upload_is_a_failure(config):
    with pytest.raises(wr.RepairFailed, match="exited 7"):
        do_repair(config, Machine(upload=(7, "000")))


def test_a_repair_is_not_done_until_the_router_answers(config):
    with pytest.raises(wr.RepairFailed, match="not both back"):
        do_repair(config, Machine(ping_code=1))


def test_a_repair_keeps_trying_the_normal_network_until_it_comes_up(config):
    class Slow(Machine):
        tries = 0

        def __call__(self, args, input_text=None, timeout=60):
            if "normal-profile" in args:
                Slow.tries += 1
                return (0 if Slow.tries == 3 else 10), ""
            return super().__call__(args, input_text, timeout)

    machine = Slow()
    do_repair(config, machine)
    assert Slow.tries == 3
    assert machine.ran("ping")


# -- the requests the firmware is strict about -------------------------------

def header_values(args):
    return [args[i + 1] for i, a in enumerate(args) if a == "-H"]


def session_for(config, machine, directory):
    return wr.WebSession(config, str(directory), machine)


def test_the_login_request_matches_what_the_unit_accepts(config, tmp_path):
    machine = Machine(login_pages=["main"])
    session_for(config, machine, tmp_path).login("pw")
    args, body = machine.commands[0], machine.inputs[0]
    assert args[-1] == "http://wifi.wavlink.com/cgi-bin/login.cgi"
    assert "wifi.wavlink.com:80:192.168.10.1" in args
    assert header_values(args) == ["Referer: http://wifi.wavlink.com/",
                                   "Origin: http://wifi.wavlink.com"]
    fields = [pair.split("=")[0] for pair in body.split("&")]
    assert fields == ["newUI", "page", "username", "langChange", "password",
                      "ipaddr", "homepage", "login_page", "hostname", "key",
                      "lang_select"]
    values = dict(pair.split("=", 1) for pair in body.split("&"))
    key = values["key"]
    assert values["password"] == hashlib.md5((key + "pw").encode()).hexdigest()
    assert values["login_page"] == "login.shtml%3F" + key
    assert values["hostname"] == "wifi.wavlink.com"
    assert values["username"] == "admin"


def test_first_time_setup_matches_what_the_unit_accepts(config, tmp_path):
    machine = Machine(login_pages=["sysinit"])
    session = session_for(config, machine, tmp_path)
    assert session.login("admin") == "sysinit"
    session.first_time_setup("pw")
    args, body = machine.commands[1], machine.inputs[1]
    assert body == ("page=sysinit&lang_select=0&wl_reddomain=US"
                    "&time_zone=UTC%2B06%3A00&newpass=pw")
    assert "Referer: http://wifi.wavlink.com/sysinit.shtml?M1" in header_values(args)


def test_the_upload_request_matches_what_the_unit_accepts(config, tmp_path):
    machine = Machine()
    session_for(config, machine, tmp_path).upload(config["settings_file"])
    args = machine.commands[0]
    assert args[-1] == "http://wifi.wavlink.com/cgi-bin/upload_settings.cgi"
    assert header_values(args) == ["Referer: http://wifi.wavlink.com/reset.shtml",
                                   "Origin: http://wifi.wavlink.com"]
    form = args[args.index("-F") + 1]
    assert form.startswith("files=@")
    assert form.endswith("/RT2880_Settings.dat;type=application/octet-stream")


# -- a check from start to finish --------------------------------------------

RESET_SCAN = "WAVLINK-N:02\\:00\\:00\\:00\\:00\\:01\n"


def do_check(config, state_path, machine, **kw):
    clock = Clock()
    said = []
    code = wr.check(config, state_path=str(state_path), run=machine,
                    sleep=clock.sleep, now=lambda: NOW, clock=clock,
                    say=said.append, **kw)
    return code, said


def test_a_check_removes_a_profile_left_by_a_killed_one(config, tmp_path):
    machine = Machine(scans=[""])
    do_check(config, tmp_path / "state.json", machine)
    assert machine.commands[0] == ["nmcli", "connection", "delete", wr.TEMP_PROFILE]


def test_an_idle_check_does_not_rewrite_an_unchanged_record(config, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"sightings": 0, "attempts": []}))
    os.utime(state_path, (1000, 1000))
    do_check(config, state_path, Machine(scans=[""]))
    assert state_path.stat().st_mtime == 1000


def test_a_sighting_is_written_down(config, tmp_path):
    state_path = tmp_path / "state.json"
    do_check(config, state_path, Machine(scans=[RESET_SCAN]))
    assert json.loads(state_path.read_text())["sightings"] == 1


def test_a_stop_request_while_repairing_is_recorded_as_a_failure(config, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"sightings": 1, "attempts": []}))

    class Stopped(Machine):
        def __call__(self, args, input_text=None, timeout=60):
            if "up" in args and wr.TEMP_PROFILE in args:
                self.commands.append(args)
                raise wr.RepairFailed("stopped by signal 15")
            return super().__call__(args, input_text, timeout)

    machine = Stopped(scans=[RESET_SCAN])
    code, _ = do_check(config, state_path, machine)
    assert code == 1
    assert "signal 15" in json.loads(state_path.read_text())["attempts"][0]["result"]
    assert machine.commands[-1][:3] == ["nmcli", "connection", "delete"]


def test_a_missing_command_reads_as_a_failed_scan(config):
    def run(args, input_text=None, timeout=60):
        return wr.run_command(["/nonexistent/command"])
    with pytest.raises(wr.RepairFailed, match="127"):
        wr.scan(config, run)


def test_a_dry_run_changes_nothing(config, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"sightings": 1, "attempts": []}))
    machine = Machine(scans=[RESET_SCAN])
    code, said = do_check(config, state_path, machine, dry_run=True)
    assert code == 0
    assert said == ["repair: factory-reset unit in range"]
    assert len(machine.commands) == 1
    assert json.loads(state_path.read_text())["attempts"] == []


def test_an_attempt_is_on_record_before_the_repair_starts(config, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"sightings": 1, "attempts": []}))

    class Dies(Machine):
        def __call__(self, args, input_text=None, timeout=60):
            if "con-name" in args:
                record = json.loads(state_path.read_text())
                assert record["attempts"][0]["result"] == "started"
                raise KeyboardInterrupt
            return super().__call__(args, input_text, timeout)

    with pytest.raises(KeyboardInterrupt):
        do_check(config, state_path, Dies(scans=[RESET_SCAN]))
    assert len(json.loads(state_path.read_text())["attempts"]) == 1


def test_a_failed_repair_is_recorded_and_reported(config, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"sightings": 1, "attempts": []}))
    code, _ = do_check(config, state_path,
                       Machine(scans=[RESET_SCAN], join_code=4))
    assert code == 1
    result = json.loads(state_path.read_text())["attempts"][0]["result"]
    assert result.startswith("failed: could not join")


def test_a_good_repair_is_recorded(config, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"sightings": 1, "attempts": []}))
    code, _ = do_check(config, state_path, Machine(scans=[RESET_SCAN]))
    assert code == 0
    record = json.loads(state_path.read_text())
    assert record["attempts"][0]["result"] == "restored"
    assert record["sightings"] == 0


def test_a_bad_settings_file_stops_a_check_before_it_scans(config, tmp_path):
    pathlib.Path(config["settings_file"]).write_text("")
    machine = Machine(scans=[RESET_SCAN])
    with pytest.raises(wr.ConfigError):
        do_check(config, tmp_path / "state.json", machine)
    assert machine.commands == []


def test_the_command_loads(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wavlink-recover"])
    namespace = runpy.run_path(str(REPO / "bin" / "wavlink-recover"),
                               run_name="not_main")
    assert callable(namespace["main"])


# -- deployment --------------------------------------------------------------

def test_the_timer_is_a_calendar_so_a_failed_check_cannot_stop_the_next():
    text = (REPO / "deploy" / "monitoring-wavlink-recover.timer").read_text()
    assert "OnCalendar=" in text
    assert "OnUnitActiveSec" not in text


def test_the_installer_only_turns_the_timer_on_with_a_config_present():
    text = (REPO / "deploy" / "install.sh").read_text()
    assert "/etc/monitoring/wavlink-recover.yaml" in text
    assert "enable --now monitoring-wavlink-recover.timer" in text
    assert "disable --now monitoring-wavlink-recover.timer" in text


def test_the_installer_refuses_the_timer_without_networkmanager():
    text = (REPO / "deploy" / "install.sh").read_text()
    assert "command -v nmcli" in text


def test_the_service_outlasts_the_longest_repair():
    text = (REPO / "deploy" / "monitoring-wavlink-recover.service").read_text()
    assert "TimeoutStartSec=20min" in text
