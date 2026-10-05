import pytest

from monitoring import drivers, lineproto
from monitoring.drivers import abc_bms
from monitoring.drivers.base import DriverError
from tests import abc_bms_fixtures as fx


class FakeTransport:
    """Answers commands from a prepared set of frames, without a radio.

    Records what was asked, because which commands get sent is part of the
    behaviour: a reading that silently stops asking for cell voltages still
    looks healthy otherwise.
    """

    def __init__(self, frames, fail=None):
        self.frames = list(frames)
        self.fail = fail
        self.asked = []
        self.closed = False

    def exchange(self, wanted):
        self.asked.extend(code for code, _ in wanted)
        if self.fail:
            raise DriverError(self.fail)
        messages = {}
        for frame in self.frames:
            if not abc_bms.frame_is_valid(frame):
                abc_bms.collect(messages, frame)
        return messages

    def close(self):
        self.closed = True


def driver(frames=(), **params):
    params.setdefault("address", "AA:BB:CC:DD:EE:FF")
    transport = FakeTransport(frames)
    params["transport"] = transport
    return abc_bms.AbcBmsDriver("pack1", params, {}), transport


def full_pack(cells=fx.SIXTEEN_CELLS):
    return [fx.status_frame()] + fx.cell_frames(cells)


# --- framing and checksums -------------------------------------------------

def test_a_command_is_six_bytes_and_carries_its_own_checksum():
    built = abc_bms.command(0xC1)
    assert len(built) == 6
    assert built[0] == abc_bms.COMMAND_HEAD and built[1] == 0xC1
    assert abc_bms.crc8(built[:-1]) == built[-1]


def test_each_command_differs_so_a_swap_cannot_go_unnoticed():
    built = {code: abc_bms.command(code) for code, _ in abc_bms.READ_COMMANDS}
    assert len(set(built.values())) == len(built)


def test_a_well_formed_reply_is_accepted():
    assert abc_bms.frame_is_valid(fx.status_frame()) is None


def test_a_reply_whose_checksum_fails_is_rejected():
    reason = abc_bms.frame_is_valid(fx.corrupt(fx.status_frame()))
    assert reason and "checksum" in reason


def test_a_reply_of_the_wrong_length_is_rejected():
    reason = abc_bms.frame_is_valid(fx.status_frame()[:-1])
    assert reason and "bytes" in reason


def test_a_reply_not_starting_with_the_response_head_is_rejected():
    stray = bytearray(fx.status_frame())
    stray[0] = 0xEE
    reason = abc_bms.frame_is_valid(bytes(stray))
    assert reason and "starts with" in reason


# --- scalar decoding -------------------------------------------------------

def test_the_scalar_values_decode_to_what_was_sent():
    messages = {}
    abc_bms.collect(messages, fx.status_frame())
    out = abc_bms.parse(messages)
    assert out["pack_volts"] == pytest.approx(53.2)
    assert out["design_capacity_ah"] == pytest.approx(100.0)
    assert out["remaining_capacity_ah"] == pytest.approx(78.0)
    assert out["cycles"] == 42.0
    assert out["state_of_charge_pct"] == 78.0


def test_a_discharging_pack_reports_negative_current():
    """Current is signed over three bytes, where an unsigned read gives a
    large positive number that is still inside any plausible range."""
    messages = {}
    abc_bms.collect(messages, fx.status_frame(amps=-12.5))
    assert abc_bms.parse(messages)["pack_amps"] == pytest.approx(-12.5)


def test_a_charging_pack_reports_positive_current():
    messages = {}
    abc_bms.collect(messages, fx.status_frame(amps=18.25))
    assert abc_bms.parse(messages)["pack_amps"] == pytest.approx(18.25)


def test_every_published_value_is_a_float():
    """InfluxDB refuses a field whose type changes between writes, so a value
    that happens to be whole must not be published as an integer."""
    out = abc_bms.parse({0xF0: fx.status_frame(cycles=42, soc=100)})
    numbers = [v for v in out.values() if not isinstance(v, str)]
    assert numbers and all(isinstance(value, float) for value in numbers)


def test_a_value_outside_its_plausible_range_is_withheld_and_named():
    """A wrong offset reads a neighbouring byte. The reading must not carry
    the number it got; it must say which field it distrusted."""
    out = abc_bms.parse({0xF0: fx.status_frame(volts=53.2, soc=200)})
    assert "state_of_charge_pct" not in out
    assert "state_of_charge_pct" in out["rejected_fields"]
    assert out["pack_volts"] == pytest.approx(53.2)


def test_a_status_frame_of_pure_noise_is_a_failure_not_a_resting_pack():
    """Current cannot be range-checked usefully, because a resting pack really
    does read near zero. So a frame of ones leaves one plausible-looking value
    behind. Voltage is the anchor: a pack that answers always has one."""
    with pytest.raises(DriverError) as raised:
        abc_bms.parse({0xF0: fx.frame(0xF0, b"\xff" * 17)})
    assert "pack_volts" in str(raised.value)


def test_a_reading_with_no_plausible_voltage_is_a_failure():
    with pytest.raises(DriverError):
        abc_bms.parse({0xF0: fx.status_frame(volts=900.0)})


def test_a_reading_with_no_frames_at_all_is_a_failure():
    with pytest.raises(DriverError):
        abc_bms.parse({})


# --- cell voltages ---------------------------------------------------------

def test_a_sixteen_cell_pack_reports_all_sixteen_cells():
    """The count comes from what arrived. A reader that assumed four cells
    would report the first frame and lose three quarters of the pack."""
    messages = {}
    for frame in fx.cell_frames(fx.SIXTEEN_CELLS):
        abc_bms.collect(messages, frame)
    assert abc_bms.cell_voltages(messages[abc_bms.CELL_MESSAGE]) == fx.SIXTEEN_CELLS


def test_a_four_cell_pack_reports_four_cells_from_the_same_reader():
    messages = {}
    for frame in fx.cell_frames(fx.FOUR_CELLS):
        abc_bms.collect(messages, frame)
    assert abc_bms.cell_voltages(messages[abc_bms.CELL_MESSAGE]) == fx.FOUR_CELLS


def test_the_published_cell_summary_follows_the_cells():
    out = abc_bms.parse({0xF0: fx.status_frame(),
                     abc_bms.CELL_MESSAGE: _joined(fx.SIXTEEN_CELLS)})
    assert out["cell_count"] == 16.0
    assert out["cell_min_volts"] == pytest.approx(min(fx.SIXTEEN_CELLS) / 1000.0)
    assert out["cell_max_volts"] == pytest.approx(max(fx.SIXTEEN_CELLS) / 1000.0)
    assert out["cell_spread_volts"] == pytest.approx(
        (max(fx.SIXTEEN_CELLS) - min(fx.SIXTEEN_CELLS)) / 1000.0)


def test_cell_frames_are_joined_while_other_messages_are_replaced():
    """Status is a snapshot, so the newest wins. Cell voltages are pages of
    one answer, so they must accumulate instead."""
    messages = {}
    abc_bms.collect(messages, fx.status_frame(soc=50))
    abc_bms.collect(messages, fx.status_frame(soc=60))
    assert abc_bms.parse(messages)["state_of_charge_pct"] == 60.0

    before = len(messages.get(abc_bms.CELL_MESSAGE, b""))
    for frame in fx.cell_frames(fx.SIXTEEN_CELLS):
        abc_bms.collect(messages, frame)
    assert len(messages[abc_bms.CELL_MESSAGE]) > before


def test_a_repeated_cell_frame_does_not_invent_extra_cells():
    """A resent frame is normal on a radio link."""
    frames = fx.cell_frames(fx.FOUR_CELLS)
    messages = {}
    for frame in frames + frames:
        abc_bms.collect(messages, frame)
    assert abc_bms.cell_voltages(messages[abc_bms.CELL_MESSAGE]) == fx.FOUR_CELLS


def test_cells_are_ordered_by_index_not_by_arrival():
    """Minimum and maximum do not care about order, but a published list of
    per-cell voltages would, and the order is the pack's own numbering."""
    messages = {}
    for frame in reversed(fx.cell_frames(fx.SIXTEEN_CELLS)):
        abc_bms.collect(messages, frame)
    assert abc_bms.cell_voltages(messages[abc_bms.CELL_MESSAGE]) == fx.SIXTEEN_CELLS


def test_cells_whose_indices_do_not_form_a_complete_run_are_discarded():
    """A pack numbers its cells from one with no gaps. Indices that skip one
    mean the entries were not where this expected them, so every voltage read
    out of that message is suspect, including the ones that looked fine."""
    import struct
    body = b"".join(struct.pack("<HH", index, 3300 + index)
                    for index in (1, 2, 3, 5))
    assert abc_bms.cell_voltages(b"\xcc\xf4" + body) == []


def test_cells_numbered_from_one_without_gaps_are_kept():
    import struct
    body = b"".join(struct.pack("<HH", index, 3300 + index)
                    for index in (1, 2, 3, 4))
    assert abc_bms.cell_voltages(b"\xcc\xf4" + body) == [3301, 3302, 3303, 3304]


def test_a_field_with_a_whole_number_scale_still_reads_as_a_float():
    field = abc_bms.Field("probe", 0xF0, 2, 1, scale=1)
    assert isinstance(field.read(b"\xcc\xf0\x05"), float)


def test_a_cell_layout_that_does_not_fit_yields_nothing_rather_than_garbage():
    """The entry layout is the one part of this protocol not confirmed against
    hardware. If the entries are not where this expects them, the indices stop
    forming a complete run, and no cell data must be published at all."""
    good = _joined(fx.SIXTEEN_CELLS)
    shifted = good[:2] + b"\x00" + good[2:]
    assert abc_bms.cell_voltages(shifted) == []


def test_a_reading_without_cell_voltages_still_publishes_the_pack():
    """Cell voltages are the uncertain part. They must not be able to take
    the certain values down with them."""
    out = abc_bms.parse({0xF0: fx.status_frame()})
    assert out["pack_volts"] == pytest.approx(53.2)
    assert "cell_count" not in out


def test_an_empty_cell_message_reports_no_cells():
    assert abc_bms.cell_voltages(b"") == []
    assert abc_bms.cell_voltages(None) == []


# --- the driver ------------------------------------------------------------

def test_the_driver_needs_an_address():
    with pytest.raises(DriverError) as raised:
        abc_bms.AbcBmsDriver("pack1", {}, {})
    assert "address" in str(raised.value)


def test_reading_asks_for_every_message_a_full_picture_needs():
    """Spelled out rather than derived from the command table, so that
    dropping a command from that table fails here instead of agreeing with
    itself. Without the detail command there are no cell voltages, and the
    reading still looks healthy."""
    device, transport = driver(full_pack())
    device.read()
    assert transport.asked == [0xC1, 0xC2, 0xC4]


def test_the_commands_between_them_cover_every_message_a_field_needs():
    covered = set()
    for _, expected in abc_bms.READ_COMMANDS:
        covered.update(expected)
    assert {field.message for field in abc_bms.FIELDS} <= covered
    assert abc_bms.CELL_MESSAGE in covered


def test_reading_a_pack_produces_the_expected_values():
    device, _ = driver(full_pack())
    out = device.read()
    assert out["pack_volts"] == pytest.approx(53.2)
    assert out["cell_count"] == 16.0


def test_a_damaged_frame_is_ignored_and_the_rest_still_reads():
    device, _ = driver(full_pack() + [fx.corrupt(fx.status_frame(soc=1))])
    assert device.read()["state_of_charge_pct"] == 78.0


def test_a_check_reports_the_pack_answering():
    identity = fx.frame(0xF1, b"SOK-48100\x00")
    device, transport = driver([identity])
    message = device.check()
    assert "AA:BB:CC:DD:EE:FF" in message and "SOK-48100" in message
    assert transport.asked == [abc_bms.IDENTITY_COMMAND[0]]


def test_a_check_against_a_silent_pack_names_the_likely_cause():
    """These modules accept one connection. The commonest reason for silence
    is a phone holding it, and the message must say so."""
    device, _ = driver([])
    with pytest.raises(DriverError) as raised:
        device.check()
    assert "one connection" in str(raised.value)


def test_raw_returns_every_frame_as_hex_for_confirming_an_offset():
    device, _ = driver(full_pack())
    out = device.raw()
    assert "0xf0" in out
    assert out["0xf0"].startswith("cc f0 ")


def test_a_transport_failure_reaches_the_caller_as_a_driver_error():
    device, _ = driver()
    device.transport = FakeTransport([], fail="out of range")
    with pytest.raises(DriverError) as raised:
        device.read()
    assert "out of range" in str(raised.value)


def test_the_driver_does_not_offer_a_resident_mode():
    """A resident reader would hold the pack's single connection open and lock
    the owner out of the vendor's application."""
    assert "resident" not in abc_bms.AbcBmsDriver.modes
    assert "poll" in abc_bms.AbcBmsDriver.modes


def test_the_driver_is_registered_under_its_configuration_name():
    assert drivers.get("abc_bms") is abc_bms.AbcBmsDriver


def test_the_older_brand_name_still_resolves_to_the_same_driver():
    """A node file is edited standing next to the hardware it describes, so a
    rename that breaks one strands a machine until somebody drives out."""
    assert drivers.get("sok") is drivers.get("abc_bms")
    assert "sok" in drivers.names()
    assert "sok" not in drivers.canonical()


def test_the_driver_declares_that_it_needs_a_radio():
    """The install script reads this to decide whether to fetch a Bluetooth
    stack. Guessing from the driver's name skipped it once already."""
    assert abc_bms.AbcBmsDriver.needs_bluetooth is True


def test_every_value_a_reading_produces_can_be_written():
    """A driver can return a type line protocol has no representation for,
    and the failure would otherwise appear as a rejected batch on the node."""
    device, _ = driver(full_pack())
    line = lineproto.line("battery", {"site": "garage", "device": "pack1"},
                          device.read())
    assert line.startswith("battery,device=pack1,site=garage ")
    assert "cell_count=16.0" in line


def _joined(cells):
    messages = {}
    for frame in fx.cell_frames(cells):
        abc_bms.collect(messages, frame)
    return messages[abc_bms.CELL_MESSAGE]
