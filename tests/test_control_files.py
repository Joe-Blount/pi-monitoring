"""The files load control reads, and the boot identity that keeps them honest."""

import json
import os
import pathlib

import pytest

from monitoring.control import clock, files, location

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


def test_a_reading_carries_the_moment_it_was_taken(tmp_path):
    """A ladder counting readings needs to tell one sample from the same
    sample read again, and the age cannot say: it moves on every call, so two
    passes over one cached sample report two different ages. The publish
    stamp does not move, so it is the sample's identity.
    """
    directory = str(tmp_path / "readings")
    files.publish_reading({"state_of_charge_pct": 45.0}, "bms",
                          directory=directory, now=100.0, boot=BOOT)
    first = files.read_readings(directory, now=130.0, this_boot=BOOT)
    second = files.read_readings(directory, now=190.0, this_boot=BOOT)
    assert first["bms"]["taken_at"] == 100.0
    assert second["bms"]["taken_at"] == first["bms"]["taken_at"]
    assert second["bms"]["age_seconds"] != first["bms"]["age_seconds"]

    files.publish_reading({"state_of_charge_pct": 44.0}, "bms",
                          directory=directory, now=280.0, boot=BOOT)
    third = files.read_readings(directory, now=290.0, this_boot=BOOT)
    assert third["bms"]["taken_at"] == 280.0


def test_an_unageable_reading_carries_no_moment(tmp_path):
    """A stamp from a clock that has restarted identifies nothing, and
    offering it would let a caller treat two unrelated samples as one."""
    directory = str(tmp_path / "readings")
    files.publish_reading({"x": 1.0}, "bms", directory=directory,
                          now=400000.0, boot=BOOT)
    out = files.read_readings(directory, now=60.0, this_boot="another-boot")
    assert out["bms"]["taken_at"] is None


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
    step = files.ADVANCE_STEP
    files.note_high_water(2000.0, path)
    assert files.note_high_water(1000.0, path) == 2000.0
    assert files.read_high_water(path) == 2000.0
    assert files.note_high_water(2000.0 + 2 * step, path) == 2000.0 + 2 * step


def test_the_high_water_mark_is_not_rewritten_for_every_passing_second(
        tmp_path):
    """This is the only file the loop writes to the card.

    The wall clock moves on every pass, so a mark rewritten whenever the
    clock is later is a mark rewritten every pass: at a ten second interval,
    thousands of fsynced writes a day on the one part of the machine with no
    spare and nothing to fall back on.

    Coarse granularity costs the check nothing, which is why it is allowed.
    What the mark catches is a board whose clock resets, and a reset clock
    does not read an hour early, it reads 1970 or the year its chip powers up
    in.
    """
    path = pathlib.Path(tmp_path) / "hw.json"
    files.note_high_water(2000.0, str(path))
    first = path.stat().st_mtime_ns

    # A loop interval's worth of passes, each with a later clock.
    for second in range(1, 200):
        assert files.note_high_water(2000.0 + second, str(path)) == 2000.0
    assert path.stat().st_mtime_ns == first, "the mark was rewritten"
    assert files.read_high_water(str(path)) == 2000.0

    # Far enough on, it does move, or it would stop tightening and a reset
    # board would be compared against the day the machine was installed.
    moved = 2000.0 + files.ADVANCE_STEP * 2
    assert files.note_high_water(moved, str(path)) == moved
    assert files.read_high_water(str(path)) == moved


def test_the_first_mark_is_written_at_once(tmp_path):
    """Until the file exists the hardware clock has nothing to be compared
    against and is trusted on sight, so the first trusted reading has to land
    whatever the granularity."""
    path = str(tmp_path / "hw.json")
    assert files.note_high_water(2000.0, path) == 2000.0
    assert files.read_high_water(path) == 2000.0


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


# -- where the machine is ----------------------------------------------------

GOOD_LOCATION = "latitude: 37.0\nlongitude: -97.4\ntimezone: America/Chicago\n"


def written(tmp_path, body):
    path = tmp_path / "location.yaml"
    path.write_text(body)
    return str(path)


def test_a_location_round_trips(tmp_path):
    out = location.read(written(tmp_path, GOOD_LOCATION))
    assert out["latitude"] == 37.0
    assert out["longitude"] == -97.4
    assert out["zone"].key == "America/Chicago"


def test_it_is_not_read_from_the_node_file(tmp_path):
    """Node files are committed and this repository is public, so the owner's
    coordinates must not be in one. The example in the repository is a nearby
    town rounded to a tenth of a degree, which gives nothing away."""
    assert location.DEFAULT_PATH.startswith("/etc/monitoring/")
    example = pathlib.Path(__file__).resolve().parent.parent / \
        "location.yaml.example"
    assert example.is_file(), "the example the error message tells people to copy"
    assert location.read(str(example))["zone"].key == "America/Chicago"


def test_a_swapped_pair_is_refused(tmp_path):
    """The mistake that looks most like a working value. This site's
    longitude read as a latitude is a point in the Arctic Ocean, and the
    schedule it produces is wrong rather than absent."""
    with pytest.raises(location.LocationError) as raised:
        location.read(written(tmp_path, "latitude: -97.4\nlongitude: 37.0\n"
                                        "timezone: America/Chicago\n"))
    assert "outside" in str(raised.value)


@pytest.mark.parametrize("body,wanted", [
    ("longitude: -97.4\ntimezone: America/Chicago\n", "needs a latitude"),
    ("latitude: 37.0\ntimezone: America/Chicago\n", "needs a longitude"),
    ("latitude: 37.0\nlongitude: -97.4\n", "IANA zone"),
])
def test_every_part_is_required(tmp_path, body, wanted):
    """None of the three has a defensible default. A missing timezone would
    place every schedule in UTC, which here is five or six hours out."""
    with pytest.raises(location.LocationError) as raised:
        location.read(written(tmp_path, body))
    assert wanted in str(raised.value)


def test_an_offset_instead_of_a_zone_name_is_refused(tmp_path):
    """The schedules have to follow daylight saving. Two days a year a fixed
    offset is an hour wrong, and an hour is most of a movement window."""
    with pytest.raises(location.LocationError) as raised:
        location.read(written(tmp_path, "latitude: 37.0\nlongitude: -97.4\n"
                                        "timezone: '-06:00'\n"))
    assert "IANA" in str(raised.value) or "not a zone" in str(raised.value)


def test_a_missing_file_says_which_file_and_what_to_copy(tmp_path):
    """Read by somebody standing at the machine, so it has to name the path
    and the thing to copy into it."""
    with pytest.raises(location.LocationError) as raised:
        location.read(str(tmp_path / "absent.yaml"))
    message = str(raised.value)
    assert "absent.yaml" in message
    assert "location.yaml.example" in message


def test_a_key_nobody_reads_is_refused(tmp_path):
    with pytest.raises(location.LocationError) as raised:
        location.read(written(tmp_path, GOOD_LOCATION + "elevation: 350\n"))
    assert "unknown key" in str(raised.value)


def test_damaged_yaml_is_refused_rather_than_read_as_nothing(tmp_path):
    """The reason has to name the real fault.

    Reading a damaged file as an empty mapping also raises, because the
    latitude is then missing, so a test asking only for an error passes
    against it. The message would send somebody looking for a key they had
    written rather than at the syntax error above it.
    """
    with pytest.raises(location.LocationError) as raised:
        location.read(written(tmp_path, "latitude: [37.0\n"))
    assert "not valid YAML" in str(raised.value)
