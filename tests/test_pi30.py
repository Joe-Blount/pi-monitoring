import pytest

from monitoring.drivers import pi30
from monitoring.drivers.base import DriverError

# Known-good frames from the published protocol, pinned here so the checksum
# is tested against the specification rather than against itself.
KNOWN_FRAMES = {
    "QPI": b"QPI\xbe\xac\r",
    "QID": b"QID\xd6\xea\r",
    "QMOD": b"QMOD\x49\xc1\r",
    "QPIGS": b"QPIGS\xb7\xa9\r",
    "QPIRI": b"QPIRI\xf8\x54\r",
    "QPIWS": b"QPIWS\xb4\xda\r",
}


@pytest.mark.parametrize("command,expected", sorted(KNOWN_FRAMES.items()))
def test_queries_match_the_published_frames(command, expected):
    assert pi30.frame(command) == expected


def test_a_reserved_crc_byte_is_incremented_rather_than_escaped():
    """The protocol's own quirk, and the reason a strictly correct checksum
    implementation fails against real hardware.

    QBOOT's checksum is 0x0a88, and 0x0a is a line feed, which is a framing
    character. The protocol increments it.
    """
    assert pi30.crc16(b"QBOOT") == 0x0A88
    assert pi30.crc_bytes(b"QBOOT") == b"\x0b\x88"


@pytest.mark.parametrize("byte", pi30.RESERVED)
def test_no_frame_may_contain_a_framing_byte_in_its_checksum(byte):
    for command in list(KNOWN_FRAMES) + ["QBOOT", "QPGS0", "QFLAG"]:
        checksum = pi30.crc_bytes(command.encode())
        assert byte not in checksum, "%s produced %r" % (command, checksum)


# -- replies -----------------------------------------------------------------

def reply(payload):
    """Build a reply the way the inverter would, checksum included."""
    body = b"(" + payload.encode()
    return body + pi30.crc_bytes(body) + b"\r"


SUNNY = ("230.1 50.0 230.1 50.0 0800 0750 015 420 53.20 010 085 0045 "
         "02.7 103.5 53.10 00000 00010101 00 02 01230")


def test_a_valid_reply_is_recognised():
    assert pi30.reply_is_valid(reply(SUNNY))


def test_a_corrupted_reply_is_recognised():
    broken = bytearray(reply(SUNNY))
    broken[5] = ord("9")
    assert not pi30.reply_is_valid(bytes(broken))


def test_the_payload_is_stripped_of_marker_checksum_and_terminator():
    assert pi30.strip(reply(SUNNY)) == SUNNY


def test_status_values_are_parsed_by_position():
    values = pi30.parse_qpigs(SUNNY)
    assert values["grid_volts"] == 230.1
    assert values["ac_out_watts"] == 750.0
    assert values["battery_volts"] == 53.2
    assert values["battery_percent"] == 85.0
    assert values["pv_volts"] == 103.5
    assert values["pv_watts"] == 1230.0


def test_heatsink_temperature_is_converted_to_fahrenheit():
    """One unit system, converted in the driver, never in the dashboard."""
    assert pi30.parse_qpigs(SUNNY)["heatsink_temp_f"] == 113.0   # 45 C
    assert "heatsink_temp_c" not in pi30.parse_qpigs(SUNNY)


def test_the_fan_offset_is_scaled_from_hundredths():
    assert pi30.parse_qpigs(SUNNY)["fan_on_offset_volts"] == 0.0


def test_device_status_bits_become_named_values():
    values = pi30.parse_qpigs(SUNNY)
    # 00010101: b4 load on, b2 charging, b0 ac charging, b1 scc charging off.
    assert values["load_on"] == 1.0
    assert values["charging_on"] == 1.0
    assert values["scc_charging_on"] == 0.0
    assert values["ac_charging_on"] == 1.0


def test_a_short_reply_is_parsed_as_far_as_it_goes():
    """Firmware revisions send different numbers of values. Discarding a whole
    reading because the last one is absent loses the ones that did arrive."""
    values = pi30.parse_qpigs("230.1 50.0 230.1 50.0 0800 0750 015")
    assert values["ac_out_watts"] == 750.0
    assert "pv_watts" not in values


def test_an_unreadable_value_does_not_cost_the_others():
    values = pi30.parse_qpigs(SUNNY.replace("0750", "----"))
    assert "ac_out_watts" not in values
    assert values["grid_volts"] == 230.1


def test_an_empty_reply_is_an_error():
    with pytest.raises(DriverError):
        pi30.parse_qpigs("   ")


def test_a_longer_reply_than_expected_does_not_break_parsing():
    values = pi30.parse_qpigs(SUNNY + " 99 88 77")
    assert values["pv_watts"] == 1230.0


def test_operating_mode_becomes_a_readable_string():
    assert pi30.parse_mode("B") == {"mode": "battery"}
    assert pi30.parse_mode("L") == {"mode": "line"}


def test_an_unknown_mode_is_passed_through_rather_than_dropped():
    assert pi30.parse_mode("Z") == {"mode": "mode_Z"}


# -- the driver, against a fake port ----------------------------------------

class FakePort:
    """Stands in for a serial port, answering the way an inverter does."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []
        self._pending = b""

    def reset_input_buffer(self):
        pass

    def write(self, data):
        command = data[:-3].decode()
        self.asked.append(command)
        self._pending = self.answers.get(command, b"")

    def read_until(self, terminator):
        out, self._pending = self._pending, b""
        return out

    def close(self):
        pass


def driver_with(answers, **params):
    params.setdefault("port", "/dev/fake")
    d = pi30.Pi30Driver("inverter", params, {})
    d._serial = FakePort(answers)
    return d


def test_a_full_read_combines_status_and_mode():
    d = driver_with({"QPIGS": reply(SUNNY), "QMOD": reply("B")})
    values = d.read()
    assert values["battery_volts"] == 53.2
    assert values["mode"] == "battery"
    assert d._serial.asked == ["QPIGS", "QMOD"]


def test_silence_names_the_likely_causes():
    """The two mistakes that cost a session: the RS485 battery jack, and the
    wrong baud rate."""
    d = driver_with({})
    with pytest.raises(DriverError) as exc:
        d.read()
    message = str(exc.value)
    assert "RS485" in message and "baud" in message


def test_a_rejected_command_says_so():
    d = driver_with({"QPIGS": b"(NAKss\r"})
    with pytest.raises(DriverError) as exc:
        d.read()
    assert "unsupported" in str(exc.value)


def test_a_bad_checksum_is_refused_and_says_how_to_relax_it():
    broken = bytearray(reply(SUNNY))
    broken[-2] ^= 0xFF
    d = driver_with({"QPIGS": bytes(broken)})
    with pytest.raises(DriverError) as exc:
        d.read()
    assert "verify_reply_crc" in str(exc.value)


def test_checksum_verification_can_be_turned_off():
    broken = bytearray(reply(SUNNY))
    broken[-2] ^= 0xFF
    d = driver_with({"QPIGS": bytes(broken)}, verify_reply_crc=False,
                    queries=["QPIGS"])
    assert d.read()["battery_volts"] == 53.2


def test_one_failed_query_does_not_lose_the_other():
    d = driver_with({"QPIGS": reply(SUNNY)}, queries=["QPIGS", "QMOD"])
    values = d.read()
    assert values["battery_volts"] == 53.2
    assert "mode" not in values


def test_check_reports_the_protocol_id():
    d = driver_with({"QPI": reply("PI30")})
    assert "PI30" in d.check()




class FakeSerialModule:
    """Stands in for pyserial, recording how the port was opened."""

    class SerialException(Exception):
        pass

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def Serial(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fail:
            raise OSError("device busy")
        return _OpenPort()


class _OpenPort:
    def read(self, _n):
        return b""

    def read_until(self, _t):
        return b""

    def write(self, _d):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


def open_with(monkeypatch, driver, fake):
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "serial", fake)
    return driver._open()


@pytest.mark.parametrize("make", [
    lambda: pi30.Pi30Driver("inv", {"port": "/dev/x", "baud": 2400,
                                    "read_timeout": 7}, {}),
    lambda: __import__("monitoring.drivers.vedirect", fromlist=["x"])
            .VedirectDriver("mppt", {"port": "/dev/x", "baud": 19200,
                                     "read_timeout": 3}, {}),
], ids=["pi30", "vedirect"])
def test_the_port_is_opened_with_a_timeout_and_exclusively(monkeypatch, make):
    """Not a grep for the text. Without the read timeout pyserial blocks
    forever, which defeats every silent-port test in the suite; without
    exclusivity two readers see torn frames that look like a cable fault."""
    fake = FakeSerialModule()
    driver = make()
    open_with(monkeypatch, driver, fake)

    (args, kwargs), = fake.calls
    assert args[0] == "/dev/x"
    assert kwargs["timeout"] == driver.read_timeout
    assert kwargs["exclusive"] is True


@pytest.mark.parametrize("make", [
    lambda: pi30.Pi30Driver("inv", {"port": "/dev/busy"}, {}),
    lambda: __import__("monitoring.drivers.vedirect", fromlist=["x"])
            .VedirectDriver("mppt", {"port": "/dev/busy"}, {}),
], ids=["pi30", "vedirect"])
def test_a_port_that_will_not_open_becomes_a_device_error(monkeypatch, make):
    """Rather than an OSError traceback, which in a resident child is the
    difference between a named device fault and a crash."""
    fake = FakeSerialModule(fail=True)
    driver = make()
    with pytest.raises(DriverError) as exc:
        open_with(monkeypatch, driver, fake)
    assert "/dev/busy" in str(exc.value)
