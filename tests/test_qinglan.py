import pytest

from monitoring import drivers, lineproto
from monitoring.drivers import qinglan
from monitoring.drivers.base import DriverError
from tests import qinglan_fixtures as fx


def build(messages=None, **params):
    params.setdefault("address", "C8:47:80:52:16:A9")
    params.setdefault("transport",
                      fx.FakeTransport(messages or fx.real_messages()))
    return qinglan.QinlanDriver("shunt", params, {})


# -- the captured shunt ----------------------------------------------------

def test_real_capture_reads_the_values_the_application_showed():
    out = qinglan.parse(fx.real_messages())
    assert out["shunt_volts"] == pytest.approx(fx.REAL_VOLTS)
    assert out["shunt_amps_abs"] == pytest.approx(fx.REAL_AMPS)
    assert out["remaining_capacity_ah"] == pytest.approx(fx.REAL_REMAINING_AH)
    assert out["state_of_charge_pct"] == pytest.approx(fx.REAL_STATE_OF_CHARGE)
    assert out["configured_capacity_ah"] == pytest.approx(fx.REAL_CAPACITY_AH)
    assert out["shunt_rating_amps"] == pytest.approx(fx.REAL_SHUNT_RATING_AMPS)


def test_the_password_record_decodes_to_the_configured_number():
    assert qinglan.password(fx.frame(fx.REAL_PASSWORD)) == 1234


def test_remaining_capacity_falls_at_the_rate_the_current_field_implies():
    """Two offsets, two scalings, one quantity. They agree or one is wrong.

    The series was captured twenty-one seconds apart while the bank
    discharged. Integrating the reported current over that interval must
    account for the drop in remaining capacity.
    """
    interval_seconds = 21.0
    records = [fx.frame(text) for text in fx.REAL_LIVE_SERIES]

    for earlier, later in zip(records, records[1:]):
        before = qinglan.parse({qinglan.LIVE: earlier})
        after = qinglan.parse({qinglan.LIVE: later})
        drawn_ah = (before["remaining_capacity_ah"]
                    - after["remaining_capacity_ah"])
        implied_amps = drawn_ah * 3600.0 / interval_seconds
        assert implied_amps == pytest.approx(before["shunt_amps_abs"], rel=0.1)


def test_every_captured_record_carries_the_configured_bus_address():
    for text in fx.REAL_LIVE_SERIES + [fx.REAL_SETTINGS, fx.REAL_ACCUMULATED]:
        record = fx.frame(text)
        assert record[1] == fx.REAL_ADDRESS


def test_captured_records_are_the_lengths_the_reader_expects():
    for command, text in ((qinglan.LIVE, fx.REAL_LIVE_SERIES[0]),
                          (qinglan.SETTINGS, fx.REAL_SETTINGS),
                          (qinglan.ACCUMULATED, fx.REAL_ACCUMULATED),
                          (qinglan.PASSWORD, fx.REAL_PASSWORD)):
        assert len(fx.frame(text)) == qinglan.FRAME_LENGTH[command]


# -- framing ---------------------------------------------------------------

def test_a_request_is_the_command_then_the_bus_address():
    assert qinglan.request(qinglan.LIVE, 1) == b"\xfb\x01"
    assert qinglan.request(qinglan.SETTINGS, 7) == b"\xfe\x07"


@pytest.mark.parametrize("address", [0, 256, -1])
def test_a_bus_address_outside_the_accepted_range_is_refused(address):
    with pytest.raises(DriverError):
        qinglan.request(qinglan.LIVE, address)


def test_a_reply_to_a_different_command_is_not_accepted():
    record = fx.live_frame()
    assert qinglan.frame_is_valid(record, qinglan.LIVE, 1) is None
    assert qinglan.frame_is_valid(record, qinglan.SETTINGS, 1) is not None


def test_a_reply_addressed_elsewhere_is_not_accepted():
    record = fx.live_frame(address=2)
    assert qinglan.frame_is_valid(record, qinglan.LIVE, 1) is not None


def test_a_truncated_reply_is_not_accepted():
    record = fx.live_frame()[:-3]
    assert qinglan.frame_is_valid(record, qinglan.LIVE, 1) is not None


# -- refusing to publish a wrong number ------------------------------------

def test_a_moved_offset_is_caught_by_the_two_ways_of_reading_charge():
    """The device's own percentage and its capacities must agree."""
    messages = {qinglan.LIVE: fx.live_frame(state_of_charge=40),
                qinglan.SETTINGS: fx.settings_frame()}
    with pytest.raises(DriverError) as raised:
        qinglan.parse(messages)
    assert "offset" in str(raised.value)


def test_a_reading_without_a_plausible_voltage_is_refused():
    with pytest.raises(DriverError):
        qinglan.parse({qinglan.LIVE: fx.live_frame(volts=0.5)})


def test_an_implausible_value_is_dropped_rather_than_published():
    out = qinglan.parse({qinglan.LIVE: fx.live_frame(amps=0.0,
                                                     remaining_ah=0.0)})
    assert out["shunt_volts"] == pytest.approx(fx.REAL_VOLTS)
    assert "rejected_fields" not in out

    # A capacity beyond anything this device can be configured for is a moved
    # offset, not a very large battery.
    out = qinglan.parse({qinglan.LIVE: fx.live_frame(),
                         qinglan.SETTINGS: fx.settings_frame(capacity_ah=6553.5)})
    assert "configured_capacity_ah" not in out
    assert "rejected_fields" in out


def test_every_published_number_is_a_float():
    """InfluxDB refuses a field whose type changes between writes."""
    out = qinglan.parse(fx.real_messages())
    for name, value in out.items():
        if name in ("rejected_fields",):
            continue
        assert isinstance(value, float), name


def test_a_reading_survives_the_line_protocol():
    out = qinglan.parse(fx.real_messages())
    line = lineproto.line("blind1", {"device": "shunt"}, out, 1)
    assert "shunt_volts=13.15" in line


# -- the driver ------------------------------------------------------------

def test_the_driver_is_registered_under_both_names():
    assert drivers.get("qinglan") is qinglan.QinlanDriver
    assert drivers.get("ble_shunt") is qinglan.QinlanDriver


def test_a_reading_sends_only_read_commands():
    transport = fx.FakeTransport(fx.real_messages())
    build(transport=transport).read()
    assert set(transport.asked) <= set(qinglan.FRAME_LENGTH)


def test_the_bus_address_defaults_to_the_factory_value():
    assert build().device_address == qinglan.DEFAULT_ADDRESS
    assert build(device_address=9).device_address == 9


def test_a_check_reports_the_shunt_without_printing_the_password():
    transport = fx.FakeTransport(
        {qinglan.PASSWORD: fx.frame(fx.REAL_PASSWORD)})
    message = build(transport=transport).check()
    assert "1234" not in message
    assert "answering" in message


def test_a_silent_shunt_names_the_bus_address_as_a_suspect():
    transport = fx.FakeTransport({})
    with pytest.raises(DriverError) as raised:
        build(transport=transport).check()
    assert "bus address" in str(raised.value)


def test_a_command_that_went_unanswered_is_published_as_a_count():
    transport = fx.FakeTransport({qinglan.LIVE: fx.live_frame()})
    out = build(transport=transport).read()
    assert out["commands_unanswered"] == 1.0


# -- the commands this driver must never send ------------------------------

#: Commands recovered from the vendor application that carry a value, and so
#: set or clear something. Among them are the factory reset, the accumulated
#: data clear and the zero-current calibration. Clearing the accumulated data
#: destroys the counter history the device exists to keep, and no reading
#: needs any of them.
WRITE_COMMANDS = (tuple(range(0xD1, 0xDF)) + (0xE0, 0xE1, 0xE2, 0xE3)
                  + (0xE6, 0xE7, 0xE8, 0xE9) + (0xEB, 0xEC, 0xED))


def test_no_write_command_appears_anywhere_in_the_driver():
    known = set(qinglan.FRAME_LENGTH) | set(qinglan.READ_COMMANDS)
    assert not known & set(WRITE_COMMANDS)


def test_a_reading_never_puts_a_write_command_on_the_wire():
    transport = fx.FakeTransport(fx.real_messages())
    driver = build(transport=transport)
    driver.read()
    driver.raw()
    assert not set(transport.asked) & set(WRITE_COMMANDS)
