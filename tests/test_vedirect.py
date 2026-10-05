import io

import pytest

from monitoring.drivers import vedirect
from tests import vedirect_fixtures as fx


def parse(data):
    """Run the frame parser over a fixed block of bytes."""
    return list(vedirect.frames(io.BytesIO(data), stop_when_empty=True))


def test_a_good_frame_parses_and_validates():
    (fields, ok), = parse(fx.frame(fx.SUNNY))
    assert ok
    assert fields["V"] == "13510" and fields["PPV"] == "300"


def test_a_corrupt_frame_is_reported_not_hidden():
    (fields, ok), = parse(fx.corrupt(fx.frame(fx.SUNNY)))
    assert not ok
    assert fields["V"] == "13510"


@pytest.mark.parametrize("byte,name", [(0x0D, "carriage return"),
                                       (0x0A, "line feed"),
                                       (0x09, "tab")])
def test_a_checksum_byte_that_is_a_separator_does_not_split_the_frame(byte, name):
    """The case that breaks line-based parsing.

    The checksum is a single raw byte and may itself be a separator. Reading
    by lines splits such a frame in two, which happens about once every 85
    frames: often enough to look like flaky hardware, rare enough to survive
    a casual test.
    """
    data = fx.frame_with_checksum_byte(fx.SUNNY, byte)
    assert data[-1] == byte
    results = parse(data)
    assert len(results) == 1, "%s checksum split the frame" % name
    fields, ok = results[0]
    assert ok and fields["V"] == "13510"


def test_several_frames_in_one_stream_are_separated():
    data = fx.frame(fx.SUNNY) + fx.frame(fx.NIGHT) + fx.frame(fx.SUNNY)
    results = parse(data)
    assert len(results) == 3
    assert all(ok for _, ok in results)
    assert results[1][0]["PPV"] == "0"


def test_a_corrupt_frame_does_not_disturb_the_next_one():
    data = fx.corrupt(fx.frame(fx.SUNNY)) + fx.frame(fx.NIGHT)
    results = parse(data)
    assert len(results) == 2
    assert results[0][1] is False
    assert results[1][1] is True


def test_attaching_mid_stream_costs_one_frame_then_resynchronises():
    """A reader that starts listening partway through a frame cannot validate
    that frame, because the bytes it missed are part of the checksum. It must
    report that one as bad and recover, rather than mis-parse or give up."""
    data = b"garbage\r\nmid\tframe" + fx.frame(fx.SUNNY) + fx.frame(fx.NIGHT)
    results = parse(data)
    assert results[0][1] is False, "a frame joined midway cannot be valid"
    assert results[-1][1] is True, "the parser must recover on the next frame"


# -- unit conversion ---------------------------------------------------------

def test_millivolts_and_milliamps_are_scaled():
    values = vedirect.normalize(dict(fx.SUNNY))
    assert values["battery_volts"] == 13.51
    assert values["battery_amps"] == 21.8
    assert values["pv_volts"] == 33.7


def test_battery_watts_is_derived():
    values = vedirect.normalize(dict(fx.SUNNY))
    assert values["battery_watts"] == round(13.51 * 21.8, 1)


def test_panel_amps_is_derived_because_the_controller_never_reports_it():
    values = vedirect.normalize(dict(fx.SUNNY))
    assert values["pv_amps"] == round(300 / 33.7, 2)


def test_panel_amps_at_night_is_zero_not_a_division_by_zero():
    """At night the array sits at zero volts, and InfluxDB cannot store the
    result of dividing by it."""
    values = vedirect.normalize(dict(fx.NIGHT))
    assert values["pv_amps"] == 0.0


def test_charge_state_and_tracker_become_readable_strings():
    assert vedirect.normalize(dict(fx.SUNNY))["charge_state"] == "bulk"
    assert vedirect.normalize(dict(fx.SUNNY))["tracker"] == "mppt_active"
    assert vedirect.normalize(dict(fx.NIGHT))["charge_state"] == "off"


def test_an_unknown_charge_state_is_passed_through_not_dropped():
    values = vedirect.normalize(dict(fx.SUNNY, CS="99"))
    assert values["charge_state"] == "cs_99"


def test_off_reason_keeps_both_the_hex_and_the_number():
    values = vedirect.normalize(dict(fx.NIGHT))
    assert values["off_reason"] == "0x00000001"
    assert values["off_reason_code"] == 1


def test_yields_are_scaled_to_kilowatt_hours():
    values = vedirect.normalize(dict(fx.SUNNY))
    assert values["yield_total_kwh"] == 90.55
    assert values["yield_today_kwh"] == 1.71


def test_a_missing_field_is_none_rather_than_an_error():
    sparse = {"V": "13000"}
    values = vedirect.normalize(sparse)
    assert values["battery_volts"] == 13.0
    assert values["pv_watts"] is None
    assert "pv_amps" not in values


def test_a_nonsense_value_does_not_cost_the_whole_frame():
    values = vedirect.normalize(dict(fx.SUNNY, PPV="---"))
    assert values["pv_watts"] is None
    assert values["battery_volts"] == 13.51


def test_identity_fields_are_available_for_bring_up():
    assert vedirect.identity(dict(fx.SUNNY)) == {
        "PID": "0xA057", "FW": "159", "SER#": "HQ25453MGTX"}


# -- the whole path, reading to line protocol --------------------------------

def test_a_frame_becomes_valid_line_protocol():
    from monitoring import lineproto
    values = vedirect.normalize(dict(fx.SUNNY))
    line = lineproto.line("blind1", {"location": "solar"}, values, 1700000000000000000)
    assert line.startswith("blind1,location=solar ")
    assert "battery_volts=13.51" in line
    assert 'charge_state="bulk"' in line
    # Every number must be a float, including ones that happen to be whole.
    assert "pv_watts=300.0" in line
    assert "error=0.0" in line
