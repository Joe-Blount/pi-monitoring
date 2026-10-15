"""The files load control reads, and the boot identity that keeps them honest."""

import json
import os

from monitoring.control import clock, files

#: A boot identity the tests control. The real one is read from /proc, which
#: does not exist on every machine the suite runs on, and a test that depended
#: on it would pass or fail by platform rather than by behaviour.
BOOT = "test-boot-identity"


def test_a_reading_round_trips_with_an_age(tmp_path):
    directory = str(tmp_path / "readings")
    files.publish_reading({"state_of_charge_pct": 45.0}, "bms",
                          directory=directory, now=100.0, boot=BOOT)
    out = files.read_readings(directory, now=130.0, this_boot=BOOT)
    assert out["bms"]["fields"]["state_of_charge_pct"] == 45.0
    assert out["bms"]["age_seconds"] == 30.0


def test_a_reading_from_another_boot_is_infinitely_old(tmp_path):
    """The monotonic clock restarts at zero every boot, so a stamp from a
    previous one cannot be aged. Reporting it as fresh would hand the ladder
    a reading from before the power cut."""
    directory = str(tmp_path / "readings")
    files.publish_reading({"x": 1.0}, "bms", directory=directory,
                          now=400000.0, boot=BOOT)
    out = files.read_readings(directory, now=60.0, this_boot="a-different-boot")
    assert out["bms"]["age_seconds"] == float("inf")


def test_a_reading_with_no_boot_identity_is_infinitely_old(tmp_path):
    directory = tmp_path / "readings"
    directory.mkdir()
    (directory / "bms.json").write_text(json.dumps(
        {"device": "bms", "fields": {"x": 1.0}, "monotonic": 10.0}))
    out = files.read_readings(str(directory), now=20.0, this_boot="boot")
    assert out["bms"]["age_seconds"] == float("inf")


def test_only_numbers_are_published_as_fields(tmp_path):
    """A driver's output carries text and booleans. The ladder compares
    numbers, and a string arriving where a threshold is expected would raise
    inside the decision rather than be refused at the edge."""
    directory = str(tmp_path / "readings")
    files.publish_reading(
        {"soc": 45.0, "note": "a reason", "charging": True, "n": 3},
        "bms", directory=directory, now=1.0, boot=BOOT)
    fields = files.read_readings(directory, now=1.0,
                                 this_boot=BOOT)["bms"]["fields"]
    assert fields == {"soc": 45.0, "n": 3}


def test_a_torn_reading_file_reads_as_absent_rather_than_crashing(tmp_path):
    """A machine can lose power between the write and the rename."""
    directory = tmp_path / "readings"
    directory.mkdir()
    (directory / "bms.json").write_text('{"device": "bms", "fields": {"x":')
    (directory / "shunt.json").write_text(json.dumps(
        {"device": "shunt", "fields": {"y": 2.0},
         "boot_id": "b", "monotonic": 5.0}))
    out = files.read_readings(str(directory), now=5.0, this_boot="b")
    assert "bms" not in out
    assert out["shunt"]["fields"]["y"] == 2.0


def test_a_missing_readings_directory_is_empty_not_an_error(tmp_path):
    assert files.read_readings(str(tmp_path / "nothing")) == {}


def test_a_write_is_never_seen_half_finished(tmp_path):
    """Rename within a directory is atomic. The temp file must also not be
    left behind, or the readings directory fills with debris."""
    path = str(tmp_path / "x.json")
    files.write_json(path, {"a": 1})
    files.write_json(path, {"a": 2})
    assert files.read_json(path) == {"a": 2}
    assert os.listdir(tmp_path) == ["x.json"]


def test_a_write_lands_by_rename_rather_than_by_copying(tmp_path, monkeypatch):
    """Deliberately coupled to the mechanism, because the guarantee has no
    black-box proxy.

    Three collectors and the control loop share this directory. Writing in
    place, or copying a temp file over the target, leaves a window where a
    reader sees half a record, and a half-read reading would reach the ladder
    as a missing field rather than as a stale one. Rename within a directory
    has no such window. A test that only checked the final contents passes
    just as well against a copy, so this asserts the call.
    """
    renamed = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (renamed.append((a, b)), real_replace(a, b))[1])
    path = str(tmp_path / "x.json")
    files.write_json(path, {"a": 1})
    assert renamed, "the file was not put in place by a rename"
    assert renamed[0][1] == path
    assert files.read_json(path) == {"a": 1}


def test_a_damaged_json_file_reads_as_none(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{not json")
    assert files.read_json(str(path)) is None
    path.write_text("[1, 2, 3]")
    assert files.read_json(str(path)) is None, "a list is not a record"


# -- loop state --------------------------------------------------------------

def test_loop_state_from_another_boot_is_discarded(tmp_path):
    """It carries monotonic stamps against a clock that has restarted at
    zero. A dwell stamped four days up would not expire for four days."""
    path = str(tmp_path / "control.json")
    files.write_state({"cameras": {"since": 400000.0}}, path, boot=BOOT)
    assert files.read_state(path, this_boot="a-different-boot") == {}


def test_loop_state_from_this_boot_survives(tmp_path):
    path = str(tmp_path / "control.json")
    files.write_state({"cameras": {"since": 10.0}}, path, boot=BOOT)
    assert files.read_state(path, this_boot=BOOT) == {
        "cameras": {"since": 10.0}}


def test_missing_loop_state_is_empty_not_none(tmp_path):
    assert files.read_state(str(tmp_path / "nothing.json")) == {}


# -- switches ----------------------------------------------------------------

def test_switches_default_to_shedding_enabled_and_no_override(tmp_path):
    out = files.read_switches(str(tmp_path / "nothing.json"))
    assert out["shedding_enabled"] is True
    assert out["loads"] == {}
    assert out["readable"] is False


def test_a_damaged_switch_file_does_not_shed_anything(tmp_path):
    """Failing closed here would let a corrupt file shed loads nobody asked
    to shed, which is a worse outcome than ignoring it and saying so."""
    path = tmp_path / "switches.json"
    path.write_text("{ broken")
    out = files.read_switches(str(path))
    assert out["shedding_enabled"] is True
    assert out["loads"] == {}
    assert out["readable"] is False


def test_switches_round_trip(tmp_path):
    path = str(tmp_path / "switches.json")
    files.write_switches({"shedding_enabled": False,
                          "loads": {"cameras": {"force": "off",
                                                "expires_epoch": 10.0}}}, path)
    out = files.read_switches(path)
    assert out["shedding_enabled"] is False
    assert out["loads"]["cameras"]["force"] == "off"
    assert out["readable"] is True


# -- the clock's high-water mark ---------------------------------------------

def test_the_high_water_mark_lives_where_a_reboot_cannot_clear_it(tmp_path):
    """The rule that catches a reset clock compares against a time already
    seen. A mark kept in tmpfs would be absent exactly when it is needed, on
    the first boot after the power cut, and a board whose clock had reset
    would be trusted with a wrong date."""
    assert files.HIGH_WATER.startswith(files.PERSIST_DIR), (
        "the high-water mark must not live in tmpfs")
    path = str(tmp_path / "hw.json")
    assert files.read_high_water(path) is None
    files.note_high_water(1000.0, path)
    assert files.read_high_water(path) == 1000.0


def test_the_high_water_mark_only_ever_advances(tmp_path):
    """A clock that has gone backwards must not drag the mark with it, or the
    check it exists for stops catching anything."""
    path = str(tmp_path / "hw.json")
    files.note_high_water(2000.0, path)
    assert files.note_high_water(1000.0, path) == 2000.0
    assert files.read_high_water(path) == 2000.0
    assert files.note_high_water(3000.0, path) == 3000.0


def test_a_damaged_high_water_file_reads_as_absent(tmp_path):
    path = tmp_path / "hw.json"
    path.write_text("{broken")
    assert files.read_high_water(str(path)) is None


# -- clock trust -------------------------------------------------------------

def test_a_synchronised_clock_is_trusted(tmp_path):
    marker = tmp_path / "synchronized"
    marker.write_text("")
    assert clock.assess(marker=str(marker), rtc=str(tmp_path / "none"))


def test_no_sync_and_no_clock_hardware_is_not_trusted(tmp_path):
    verdict = clock.assess(marker=str(tmp_path / "none"),
                           rtc=str(tmp_path / "none"))
    assert not verdict
    assert "no clock hardware" in verdict.reason


def test_clock_hardware_that_cannot_be_read_is_not_trusted(tmp_path):
    """A module with a dead coin cell registers its device and then fails the
    read. The device node existing is not evidence the clock is right."""
    rtc = tmp_path / "since_epoch"
    rtc.write_text("")
    assert not clock.assess(marker=str(tmp_path / "none"), rtc=str(rtc))


def test_clock_hardware_reading_before_a_time_already_seen_is_not_trusted(
        tmp_path):
    """A board whose clock resets on a power cut reads below the high-water
    mark. This is the fixed-date check made sound: the bound is the machine's
    own last known time rather than a constant."""
    rtc = tmp_path / "since_epoch"
    rtc.write_text("1000000")
    verdict = clock.assess(high_water=2000000.0,
                           marker=str(tmp_path / "none"), rtc=str(rtc))
    assert not verdict
    assert "earlier than a time already seen" in verdict.reason


def test_clock_hardware_reading_after_a_time_already_seen_is_trusted(tmp_path):
    rtc = tmp_path / "since_epoch"
    rtc.write_text("3000000")
    assert clock.assess(high_water=2000000.0,
                        marker=str(tmp_path / "none"), rtc=str(rtc))


def test_synchronisation_outranks_a_backwards_clock(tmp_path):
    """Once the daemon has synchronised, the system clock is right whatever
    the hardware says, and the hardware is about to be written from it."""
    marker = tmp_path / "synchronized"
    marker.write_text("")
    rtc = tmp_path / "since_epoch"
    rtc.write_text("1")
    assert clock.assess(high_water=2000000.0, marker=str(marker), rtc=str(rtc))
