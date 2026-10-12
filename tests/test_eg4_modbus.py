import pytest

from monitoring import drivers, lineproto
from monitoring.drivers import eg4_modbus
from monitoring.drivers.base import DriverError
from tests import eg4_modbus_fixtures as fx


def build(replies=None, **params):
    params.setdefault("port", "/dev/fake")
    params.setdefault("transport", fx.FakeTransport(
        replies if replies is not None else [fx.reply(fx.PV_ON_RECHARGING)]))
    return eg4_modbus.Eg4ModbusDriver("inverter", params, {})


# -- the captured inverter --------------------------------------------------

def test_the_compressor_sample_reads_what_the_hardware_reported():
    out = eg4_modbus.parse(fx.PV_OFF_COMPRESSOR)
    assert out["battery_volts"] == pytest.approx(56.2)
    assert out["battery_amps"] == pytest.approx(-25.2)
    assert out["battery_watts"] == pytest.approx(-1416.0)
    assert out["load_watts"] == pytest.approx(1398.0)
    assert out["pv_watts"] == 0.0
    assert out["ac_out_volts"] == pytest.approx(120.1)
    assert out["ac_out_hz"] == pytest.approx(60.01)


def test_the_battery_sign_is_proved_by_two_states_not_assumed():
    """The array was disconnected and reconnected to establish this.

    A sign taken on faith is the kind of error that reads perfectly on a
    dashboard while being backwards.
    """
    discharging = eg4_modbus.parse(fx.PV_OFF_COMPRESSOR)
    charging = eg4_modbus.parse(fx.PV_ON_RECHARGING)
    assert discharging["battery_amps"] < 0 < charging["battery_amps"]
    assert discharging["battery_watts"] < 0 < charging["battery_watts"]


def test_volts_times_amps_equals_watts_in_every_captured_state():
    """Three registers that can only agree if all three are where this
    driver thinks they are."""
    for name, registers in fx.ALL.items():
        out = eg4_modbus.parse(registers)
        for watts, volts, amps in eg4_modbus.POWER_CHECKS:
            if watts not in out:
                continue
            expected = out[volts] * out[amps]
            assert expected == pytest.approx(
                out[watts], abs=eg4_modbus.POWER_TOLERANCE_WATTS,
                rel=eg4_modbus.POWER_TOLERANCE_PCT / 100.0), (
                "%s: %s" % (name, watts))


def test_the_array_reads_exactly_zero_when_it_is_disconnected():
    """How PV was told apart from load in the first place: with the array
    carrying the whole load, the two are indistinguishable."""
    for registers in (fx.PV_OFF_IDLE, fx.PV_OFF_COMPRESSOR):
        out = eg4_modbus.parse(registers)
        assert out["pv_watts"] == 0.0
        assert out["pv_amps"] == 0.0


def test_every_captured_state_parses_without_being_refused():
    for name, registers in fx.ALL.items():
        out = eg4_modbus.parse(registers)
        assert "battery_volts" in out, name
        assert "rejected_fields" not in out, name


def test_every_published_number_is_a_float():
    """InfluxDB refuses a field whose type changes between writes."""
    out = eg4_modbus.parse(fx.PV_ON_RECHARGING)
    for name, value in out.items():
        if name == "rejected_fields":
            continue
        assert isinstance(value, float), name


def test_a_reading_survives_the_line_protocol():
    out = eg4_modbus.parse(fx.PV_OFF_COMPRESSOR)
    line = lineproto.line("garage", {"device": "inverter"}, out, 1)
    assert "battery_watts=-1416" in line


# -- refusing to publish a wrong number -------------------------------------

def test_a_shifted_register_map_is_refused_rather_than_published():
    """A firmware that moves the map by one is the failure this guards.

    Every number stays inside its own plausible range, so range checks alone
    see nothing wrong. Only volts times amps against watts catches it.
    """
    # Current and power transposed, which is the shape a map shift takes
    # here: both stay inside their own ranges and only their product gives
    # it away. A whole-block shift is caught by the voltage anchor instead,
    # which is a different guard and is tested separately.
    moved = dict(fx.PV_OFF_COMPRESSOR)
    moved[216] = 0x10000 - 50          # -5.0 A, perfectly plausible on its own
    with pytest.raises(DriverError) as raised:
        eg4_modbus.parse(moved)
    assert "register map" in str(raised.value)


def test_a_whole_block_shift_is_caught_by_the_voltage_anchor():
    shifted = {number - 1: value
               for number, value in fx.PV_OFF_COMPRESSOR.items()}
    with pytest.raises(DriverError) as raised:
        eg4_modbus.parse(shifted)
    assert "battery voltage" in str(raised.value)


def test_a_reading_without_a_plausible_battery_voltage_is_refused():
    registers = dict(fx.PV_ON_IDLE)
    registers[215] = 5
    with pytest.raises(DriverError):
        eg4_modbus.parse(registers)


def test_an_implausible_value_is_dropped_rather_than_published():
    registers = dict(fx.PV_ON_IDLE)
    registers[207] = 60000           # 600 Hz is a moved register, not a grid
    out = eg4_modbus.parse(registers)
    assert "ac_out_hz" not in out
    assert "rejected_fields" in out
    assert out["battery_volts"] == pytest.approx(57.0)


# -- framing ----------------------------------------------------------------

def test_a_request_asks_only_for_holding_registers():
    frame = eg4_modbus.request(1, 205, 19)
    assert frame[0] == 1
    assert frame[1] == eg4_modbus.READ_HOLDING
    assert eg4_modbus.crc16(frame[:-2]) == frame[-2:]


def test_the_checksum_matches_a_frame_the_hardware_accepted():
    """Captured from the exchange that first got an answer out of it."""
    assert eg4_modbus.crc16(bytes([1, 3, 0, 0, 0, 1])) == bytes([0x84, 0x0A])


@pytest.mark.parametrize("damage,reason", [
    (lambda r: r[:4], "short"),
    (lambda r: bytes([2]) + r[1:], "unit"),
    (lambda r: r[:1] + bytes([0x83, 0x02]) + r[3:], "exception"),
    (lambda r: r[:-1] + bytes([r[-1] ^ 0xFF]), "checksum"),
])
def test_a_damaged_reply_is_rejected_with_a_reason(damage, reason):
    good = fx.reply(fx.PV_ON_IDLE)
    registers, why = eg4_modbus.parse_reply(
        damage(good), 1, eg4_modbus.BLOCK_START, eg4_modbus.BLOCK_COUNT)
    assert registers is None
    assert reason in why


def test_a_good_reply_decodes_to_the_registers_that_went_in():
    registers, why = eg4_modbus.parse_reply(
        fx.reply(fx.PV_OFF_COMPRESSOR), 1,
        eg4_modbus.BLOCK_START, eg4_modbus.BLOCK_COUNT)
    assert why is None
    for number, value in fx.PV_OFF_COMPRESSOR.items():
        assert registers[number] == value


# -- the driver -------------------------------------------------------------

def test_the_driver_is_registered():
    assert drivers.get("eg4_modbus") is eg4_modbus.Eg4ModbusDriver


def test_a_reading_sends_one_request_and_decodes_it():
    transport = fx.FakeTransport([fx.reply(fx.PV_ON_RECHARGING)])
    out = build(transport=transport).read()
    assert len(transport.sent) == 1
    assert out["pv_watts"] == pytest.approx(1774.0)


def test_a_damaged_reply_is_retried_before_it_is_a_failure():
    """A serial line picks up noise. One bad frame is not a dead inverter."""
    transport = fx.FakeTransport([b"\x01\x03\xff", fx.reply(fx.PV_ON_IDLE)])
    out = build(transport=transport).read()
    assert len(transport.sent) == 2
    assert out["battery_volts"] == pytest.approx(57.0)


def test_a_silent_inverter_names_the_things_worth_checking():
    transport = fx.FakeTransport([b"", b"", b""])
    with pytest.raises(DriverError) as raised:
        build(transport=transport).read()
    message = str(raised.value)
    assert "RS232" in message and "PI30" in message


def test_the_driver_never_sends_a_write_function():
    """Registers next to these hold charge voltages. A wrong write there
    changes how the battery is charged, silently."""
    transport = fx.FakeTransport([fx.reply(fx.PV_ON_IDLE)] * 4)
    driver = build(transport=transport)
    driver.read()
    driver.check()
    driver.raw()
    assert transport.sent, "nothing was sent, so this proves nothing"
    for frame in transport.sent:
        assert frame[1] == eg4_modbus.READ_HOLDING


def test_a_check_reports_the_battery_voltage_it_actually_read():
    transport = fx.FakeTransport([fx.reply(fx.PV_OFF_COMPRESSOR)])
    assert "56.2 V" in build(transport=transport).check()
