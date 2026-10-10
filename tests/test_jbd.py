import pytest

from monitoring import drivers, lineproto
from monitoring.drivers import jbd
from monitoring.drivers.base import DriverError
from tests import jbd_fixtures as fx


class FakeTransport:
    """Answers registers from prepared frames, with no radio."""

    def __init__(self, notifications, fail=None):
        self.notifications = list(notifications)
        self.fail = fail
        self.asked = []

    def exchange(self, registers):
        self.asked.extend(registers)
        if self.fail:
            raise DriverError(self.fail)
        messages, buffer = {}, bytearray()
        for chunk in self.notifications:
            buffer.extend(chunk)
            complete, rest = jbd.frames(buffer)
            buffer[:] = rest
            for frame in complete:
                if jbd.frame_is_valid(frame) is None:
                    messages[frame[1]] = jbd.payload(frame)
        return messages

    def close(self):
        pass


def driver(notifications=(), **params):
    params.setdefault("address", "AA:BB:CC:DD:EE:FF")
    transport = FakeTransport(notifications)
    params["transport"] = transport
    return jbd.JbdDriver("bms", params, {}), transport


def whole_pack():
    return [fx.basic_frame(), fx.cells_frame(fx.REAL_CELL_MILLIVOLTS),
            fx.hardware_frame()]


# --- framing, which is where this protocol differs ------------------------

def test_a_reply_split_across_notifications_is_reassembled():
    """The real basic-information reply arrives as three packets because it is
    longer than one Bluetooth frame. A reader that treats each notification as
    a frame sees a valid-looking first fragment and loses two thirds of the
    fields without any error."""
    messages = fx.real_messages()
    assert jbd.BASIC in messages
    assert len(messages[jbd.BASIC]) == 0x22


def test_a_partial_frame_is_held_until_the_rest_arrives():
    whole = fx.basic_frame()
    done, rest = jbd.frames(whole[:10])
    assert done == [] and rest == whole[:10]

    done, rest = jbd.frames(whole)
    assert len(done) == 1 and rest == b""


def test_two_replies_in_one_buffer_are_both_returned():
    done, rest = jbd.frames(fx.cells_frame([3300, 3301]) + fx.hardware_frame("X"))
    assert [f[1] for f in done] == [jbd.CELLS, jbd.HARDWARE]
    assert rest == b""


def test_a_stray_start_byte_does_not_swallow_the_frame_after_it():
    """Noise on the wire should cost the noise, not the next good reply."""
    good = fx.cells_frame([3300, 3301])
    done, _ = jbd.frames(b"\xdd\x99\x00\x02\x00" + good)
    assert any(jbd.frame_is_valid(f) is None and f[1] == jbd.CELLS for f in done)


def test_bytes_before_the_first_start_marker_are_discarded():
    good = fx.hardware_frame("ABC")
    done, _ = jbd.frames(b"\x01\x02\x03" + good)
    assert len(done) == 1 and jbd.payload(done[0]) == b"ABC"


# --- validation -----------------------------------------------------------

def test_a_well_formed_reply_is_accepted():
    assert jbd.frame_is_valid(fx.basic_frame()) is None


def test_a_corrupt_checksum_is_rejected():
    reason = jbd.frame_is_valid(fx.corrupt(fx.basic_frame()))
    assert reason and "checksum" in reason


def test_a_pack_reporting_an_error_status_is_rejected():
    """Byte two is a status, not padding. A non-zero value means the pack
    refused the request, and its payload is not an answer."""
    reason = jbd.frame_is_valid(fx.frame(jbd.BASIC, b"\x00" * 23, status=0x80))
    assert reason and "status" in reason


def test_a_length_that_disagrees_with_the_frame_is_rejected():
    broken = bytearray(fx.cells_frame([3300, 3301]))
    broken[3] = 0x20
    reason = jbd.frame_is_valid(bytes(broken))
    assert reason and "payload bytes" in reason


def test_a_command_carries_a_correct_checksum():
    built = jbd.command(jbd.BASIC)
    assert built[0] == jbd.START and built[1] == jbd.REQUEST and built[-1] == jbd.END
    assert int.from_bytes(built[4:6], "big") == jbd.checksum(built[2:4])


# --- decoding -------------------------------------------------------------

def test_the_captured_frames_decode_to_what_the_pack_reported():
    """Captured from a Chins 320 Ah pack. The cross-check that matters is the
    last one: four cells summing to 13.391 V against a reported 13.37 V means
    the cell layout and the voltage scale are both right."""
    out = jbd.parse(fx.real_messages())
    assert out["pack_volts"] == 13.37
    assert out["pack_amps"] == 7.21
    assert out["design_capacity_ah"] == 320.0
    assert out["remaining_capacity_ah"] == 169.08
    assert out["cycles"] == 85.0
    assert out["state_of_charge_pct"] == 53.0
    assert out["cell_count"] == 4.0
    assert out["charge_mosfet_on"] == 1.0 and out["discharge_mosfet_on"] == 1.0
    assert out["protection_flags"] == 0.0

    cells = jbd.cell_voltages(fx.real_messages()[jbd.CELLS])
    assert cells == fx.REAL_CELL_MILLIVOLTS
    assert abs(sum(cells) / 1000.0 - out["pack_volts"]) < 0.05


def test_everything_is_read_big_endian():
    """The other battery driver in this project is little endian. Reading this
    one the same way gives numbers that are wrong by orders of magnitude and
    still look like numbers."""
    out = jbd.parse({jbd.BASIC: jbd.payload(fx.basic_frame(volts=13.37))})
    assert out["pack_volts"] == pytest.approx(13.37)


def test_a_discharging_pack_reads_negative():
    """Current is signed. Unsigned, a discharge reads as about 650 amps."""
    out = jbd.parse({jbd.BASIC: jbd.payload(fx.basic_frame(amps=-18.4))})
    assert out["pack_amps"] == pytest.approx(-18.4)


def test_the_temperature_is_converted_from_tenths_of_a_kelvin():
    """2963 tenths of a Kelvin is 23.2 C, which is 73.8 F."""
    out = jbd.parse({jbd.BASIC: jbd.payload(fx.basic_frame(temps=(23.2,)))})
    assert out["temp_1"] == 73.8


def test_a_pack_with_several_sensors_reports_all_of_them():
    """The count says how many follow, so this is read rather than assumed."""
    out = jbd.parse({jbd.BASIC: jbd.payload(
        fx.basic_frame(temps=(20.0, 25.0, 30.0)))})
    assert out["temperature_sensors"] == 3.0
    assert [out["temp_1"], out["temp_2"], out["temp_3"]] == [68.0, 77.0, 86.0]


def test_the_switch_states_are_read_as_separate_bits():
    """One byte holds both. A pack that has stopped accepting charge but still
    supplies the load is the failure that matters, and it is invisible unless
    the two bits are read apart."""
    out = jbd.parse({jbd.BASIC: jbd.payload(fx.basic_frame(fets=0x01))})
    assert out["charge_mosfet_on"] == 1.0 and out["discharge_mosfet_on"] == 0.0

    out = jbd.parse({jbd.BASIC: jbd.payload(fx.basic_frame(fets=0x02))})
    assert out["charge_mosfet_on"] == 0.0 and out["discharge_mosfet_on"] == 1.0


def test_a_protection_fault_is_published_as_a_number():
    """Alertable without the dashboard needing to know the bit meanings."""
    out = jbd.parse({jbd.BASIC: jbd.payload(fx.basic_frame(protection=0x0020))})
    assert out["protection_flags"] == 32.0


def test_every_published_value_is_a_float():
    out = jbd.parse(fx.real_messages())
    numbers = [v for v in out.values() if not isinstance(v, str)]
    assert numbers and all(isinstance(v, float) for v in numbers)


def test_an_implausible_value_is_withheld_and_named():
    out = jbd.parse({jbd.BASIC: jbd.payload(fx.basic_frame(soc=200))})
    assert "state_of_charge_pct" not in out
    assert "state_of_charge_pct" in out["rejected_fields"]


def test_a_frame_with_no_plausible_voltage_is_a_failure():
    """Current reads near zero on a resting pack, so without the voltage
    anchor a frame of noise leaves a believable value behind."""
    with pytest.raises(DriverError):
        jbd.parse({jbd.BASIC: b"\x00" * 23})


def test_a_reading_without_basic_information_is_a_failure():
    with pytest.raises(DriverError):
        jbd.parse({jbd.CELLS: jbd.payload(fx.cells_frame([3300, 3301]))})


# --- cells ----------------------------------------------------------------

def test_an_odd_number_of_bytes_is_not_cell_data():
    """Two bytes per cell, so an odd length means this is not what it claims."""
    assert jbd.cell_voltages(b"\x0d\x1a\x0d") == []


def test_a_voltage_outside_a_cell_range_rejects_the_whole_reading():
    """Position is the only thing identifying a cell here, so one bad value
    means the alignment is wrong and none of them can be trusted."""
    body = jbd.payload(fx.cells_frame([3354, 99, 3351, 3342]))
    assert jbd.cell_voltages(body) == []


def test_a_sixteen_cell_pack_reads_sixteen_cells():
    cells = [3300 + i for i in range(16)]
    assert jbd.cell_voltages(jbd.payload(fx.cells_frame(cells))) == cells


def test_one_cell_is_not_a_battery():
    assert jbd.cell_voltages(jbd.payload(fx.cells_frame([3300]))) == []


# --- the driver -----------------------------------------------------------

def test_the_driver_needs_an_address():
    with pytest.raises(DriverError) as raised:
        jbd.JbdDriver("bms", {}, {})
    assert "address" in str(raised.value)


def test_reading_asks_for_every_register_a_full_picture_needs():
    """Spelled out rather than derived from the constant, so dropping one
    fails here instead of agreeing with itself."""
    device, transport = driver(whole_pack())
    device.read()
    assert transport.asked == [0x03, 0x04, 0x05]


def test_a_check_names_the_pack_from_its_hardware_string():
    device, transport = driver([fx.hardware_frame()])
    message = device.check()
    assert fx.REAL_HARDWARE in message
    assert transport.asked == [jbd.HARDWARE]


def test_a_check_against_a_silent_pack_names_the_likely_cause():
    device, _ = driver([])
    with pytest.raises(DriverError) as raised:
        device.check()
    assert "one connection" in str(raised.value)


def test_a_register_that_brought_no_reply_is_counted():
    device, _ = driver([fx.basic_frame()])
    assert device.read()["commands_unanswered"] == 2.0


def test_a_damaged_frame_is_ignored_and_the_rest_still_reads():
    device, _ = driver(whole_pack() + [fx.corrupt(fx.basic_frame(soc=1))])
    assert device.read()["state_of_charge_pct"] == 53.0


def test_the_driver_does_not_offer_a_resident_mode():
    """A resident reader would hold the pack's single connection open and lock
    the owner out of their application."""
    assert "resident" not in jbd.JbdDriver.modes


def test_the_driver_declares_what_it_needs():
    assert jbd.JbdDriver.needs_bluetooth is True
    assert jbd.JbdDriver.slow_read is True


def test_the_driver_is_registered_and_the_old_name_still_resolves():
    assert drivers.get("jbd") is jbd.JbdDriver
    assert drivers.get("ble_bms") is jbd.JbdDriver
    assert "ble_bms" not in drivers.canonical()


def test_a_reading_renders_as_line_protocol():
    device, _ = driver(whole_pack())
    line = lineproto.line("blind1", {"device": "bms"}, device.read())
    assert line.startswith("blind1,device=bms ")
    assert "cell_count=4.0" in line


def test_a_timeout_written_as_a_duration_is_accepted():
    """Node files write 40s everywhere else. A driver that demands a bare
    float turns a consistent file into a device that cannot be built, and the
    error says nothing about which device or which field."""
    device, _ = driver(whole_pack(), timeout="40s", settle="2s")
    assert device.timeout == 40.0
    assert device.settle == 2.0


def test_a_timeout_written_as_a_number_still_works():
    device, _ = driver(whole_pack(), timeout=25, settle=1.5)
    assert device.timeout == 25.0 and device.settle == 1.5


def test_an_unparseable_timeout_names_the_device_and_the_field():
    with pytest.raises(Exception) as raised:
        jbd.JbdDriver("bms", {"address": "A", "timeout": "soon"}, {})
    assert "bms" in str(raised.value) and "timeout" in str(raised.value)
