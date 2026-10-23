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

#: CRC-8/MAXIM over the ASCII digits 1 to 9, from the published CRC
#: catalogue rather than from any code here.
CRC8_MAXIM_CHECK = 0xA1


def test_the_checksum_is_crc8_maxim():
    assert abc_bms.crc8(b"123456789") == CRC8_MAXIM_CHECK


def test_the_fixture_checksum_is_crc8_maxim_as_well():
    """The fixtures build every reply with their own checksum, so a driver
    checksum that drifted would reject them. That only holds while the
    fixture's own is right."""
    assert fx.crc8(b"123456789") == CRC8_MAXIM_CHECK


@pytest.mark.parametrize("code, expected", [
    (0xC0, "ee c0 00 00 00 41"),
    (0xC1, "ee c1 00 00 00 ce"),
    (0xC2, "ee c2 00 00 00 46"),
    (0xC4, "ee c4 00 00 00 4f"),
])
def test_a_command_is_the_head_the_code_three_zeros_and_a_checksum(
        code, expected):
    assert abc_bms.command(code) == bytes.fromhex(expected)


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
    assert abc_bms.cell_voltages(_cell_message((1, 2, 3, 4))) == \
        [3301, 3302, 3303, 3304]


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
    assert transport.asked == [0xC0]


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


# --- cell numbering, which the references disagree about -------------------

def _cell_message(indices, millivolts=3300):
    import struct
    body = b"".join(bytes([i]) + struct.pack("<H", millivolts + i) + b"\x00"
                    for i in indices)
    return b"\xcc\xf4" + body


def test_a_pack_numbering_its_cells_from_zero_reports_every_cell():
    """Read as one-based, a zero-based pack loses its first cell and the rest
    still form a complete run, so the count, the minimum and the spread all
    come out plausible and wrong. That is the failure this reader exists to
    prevent, so both numberings are accepted."""
    cells = abc_bms.cell_voltages(_cell_message(range(0, 16)))
    assert len(cells) == 16
    assert cells[0] == 3300


def test_a_pack_numbering_its_cells_from_one_reports_every_cell():
    cells = abc_bms.cell_voltages(_cell_message(range(1, 17)))
    assert len(cells) == 16
    assert cells[0] == 3301


def test_indices_that_start_at_two_are_refused():
    """Neither numbering starts there, so the entries are not where this
    expected them and every value read out is suspect."""
    assert abc_bms.cell_voltages(_cell_message(range(2, 18))) == []


def test_a_single_plausible_entry_is_not_a_one_cell_battery():
    """One byte pair falling inside the plausible range is a coincidence. A
    pack of one cell does not exist."""
    assert abc_bms.cell_voltages(_cell_message([1])) == []


# --- a pack that answers some commands and not others ---------------------

def test_a_command_that_brought_no_reply_is_counted():
    """Comparing against every message gathered so far meant that once any
    command had answered, a later silent one could never be noticed. The
    reading then looked complete with a third of its fields missing."""
    out = abc_bms.parse({0xF0: fx.status_frame(),
                         abc_bms.UNANSWERED: [0xC2, 0xC4]})
    assert out["commands_unanswered"] == 2.0


def test_a_fully_answered_reading_says_so_explicitly():
    """Published even when it is zero, so that a dashboard can alert on the
    field rising rather than on a field appearing."""
    out = abc_bms.parse({0xF0: fx.status_frame()})
    assert out["commands_unanswered"] == 0.0


def test_mosfet_states_without_a_voltage_are_not_a_reading():
    """A pack whose F0 frames all fail their checksum while F2 passes would
    otherwise publish switch states and claim the pack was read."""
    with pytest.raises(DriverError):
        abc_bms.parse({0xF2: fx.frame(0xF2, b"\x01\x01\x02")})


def test_raw_still_shows_the_frames_when_a_command_went_unanswered():
    """A pack that answers some commands and not others is the case raw mode
    exists for. The unanswered record shares the dict with the frames but is
    keyed by name, so sorting and formatting every key as a number threw the
    frames away behind a traceback."""
    device, transport = driver(full_pack())
    transport.exchange = lambda wanted: {
        0xF0: fx.status_frame(),
        abc_bms.UNANSWERED: [0xC2, 0xC4],
    }
    out = device.raw()
    assert out["0xf0"].startswith("cc f0 ")
    assert out["unanswered_commands"] == "0xc2, 0xc4"


def test_raw_omits_the_unanswered_line_when_everything_answered():
    device, transport = driver(full_pack())
    transport.exchange = lambda wanted: {0xF0: fx.status_frame()}
    assert "unanswered_commands" not in device.raw()


# --- against frames captured from a real pack ------------------------------

def test_the_driver_agrees_with_the_vendor_application():
    """Generated fixtures only prove the parser agrees with itself. These
    frames came off a SOK-48V0045 while its own application was displaying
    the values asserted below, so this is the test that proves the offsets
    are right rather than merely self-consistent.

    The three that cannot drift are the ones that matter: rated capacity,
    actual capacity and cycle count were identical on both screens."""
    out = abc_bms.parse(fx.real_messages())

    assert out["design_capacity_ah"] == 100.0        # app: Rated 100.00 Ah
    assert out["remaining_capacity_ah"] == 106.048   # app: Actual 106.05 Ah
    assert out["cycles"] == 212.0                    # app: Cycle Time 212
    assert out["charge_mosfet_on"] == 1.0            # app: C MOS on
    assert out["discharge_mosfet_on"] == 1.0         # app: D MOS on
    assert out["heater_on"] == 0.0                   # app: Heating Switch OFF
    assert out["temperature_sensors"] == 2.0         # app showed two


def test_the_captured_temperatures_match_what_the_application_showed():
    """25 C and 24 C on screen, 19 00 18 00 in the frame. Published in
    Fahrenheit because everything in this project is."""
    out = abc_bms.parse(fx.real_messages())
    assert out["temp_1"] == 77.0
    assert out["temp_2"] == 75.2


def test_every_captured_cell_is_read_at_its_real_voltage():
    """The layout was wrong before this capture: a one-byte index was read as
    two, so nothing decoded and the reader published no cells at all. That was
    the designed behaviour, and this is what replaces the guess."""
    messages = fx.real_messages()
    assert abc_bms.cell_voltages(messages[abc_bms.CELL_MESSAGE]) == \
        fx.REAL_CELL_MILLIVOLTS

    out = abc_bms.parse(messages)
    assert out["cell_count"] == 16.0
    assert out["cell_min_volts"] == 3.292
    assert out["cell_max_volts"] == 3.304


def test_a_discharging_pack_reads_negative_against_a_real_frame():
    """The application showed a negative current while discharging, so the
    sign convention here matches the vendor's."""
    assert abc_bms.parse(fx.real_messages())["pack_amps"] < 0


def test_the_captured_identity_frame_names_the_pack():
    device, transport = driver([])
    transport.exchange = lambda wanted: {
        0xF1: bytes.fromhex(fx.REAL[0].replace(" ", ""))}
    assert "SOK-BMS" in device.check()


def test_the_alarm_message_is_published_rather_than_discarded():
    """The read sends command 0xC4 on every cycle, so the reply it brings
    back costs nothing extra and has to reach the database. That reply is
    where cell over-voltage, under-voltage, over-temperature and over-current
    faults live, which makes it the single most important thing the pack
    says."""
    out = abc_bms.parse(fx.real_messages())
    assert out["alarm_flags"] == 0.0


def protection_frame(*active):
    """A protection reply with the named states set and the rest clear."""
    names = list(VENDOR_PROTECTION_ORDER)
    payload = bytearray(len(names))
    for name in active:
        payload[names.index(name)] = VENDOR_PROTECTION_ACTIVE
    return fx.frame(abc_bms.ALARM_MESSAGE, bytes(payload))


# -- what the pack sends, written out ---------------------------------------
#
# These four describe the protection reply the way the pack produces it, and
# they are literals on purpose. A fixture or an assertion that takes any of
# them from the decoder moves whenever the decoder moves, so the two agree
# all the way to a pack whose faults read as healthy. Each of the four is a
# value a test would naturally reach into the decoder for, and doing so fails
# silently: the suite stays green and the pack is misread.
#
# They come from the vendor application: its decompiled source assigns one
# state per byte over a fixed span of the reply and tests each for an exact
# value, and its protection screen lists the states in this order under
# Charge, Discharge and Hardware.
#
# The decoder is compared against each of them, and nothing else that builds
# or checks a protection reply reaches into the decoder for a position, a
# value or an order. That holds for the protection reply only: elsewhere in
# this file the frame head, the identity command and the checksum are still
# taken from the decoder and used as their own expectations.

#: The reply that carries the protection states.
VENDOR_ALARM_MESSAGE = 0xF9

#: Where the first state sits, counted from the start of the frame: byte 2,
#: straight after the frame head and the message identifier.
VENDOR_PROTECTION_OFFSET = 2

#: What the pack puts in a state's byte when that state is set.
VENDOR_PROTECTION_ACTIVE = 0x01

#: The order the states arrive in, one byte each from the offset above.
VENDOR_PROTECTION_ORDER = (
    "COV", "FC", "SOCC", "OTC", "UTC", "BOTC",
    "CUV", "FD", "SOCD", "OTD", "UTD", "BOTD",
    "SCD", "OCD", "UVP", "OVP",
)


def test_the_protection_reply_is_the_one_the_pack_answers_with():
    """Every protection test here builds and keys its frame by the decoder's
    message id, so all of them would follow it if it changed. One captured
    frame pins it, but only incidentally, in a test about publishing. This
    makes the pin deliberate."""
    assert abc_bms.ALARM_MESSAGE == VENDOR_ALARM_MESSAGE


def test_the_first_state_sits_where_the_pack_puts_it():
    """Same reason as the byte and the order: pinned against the pack rather
    than against the decoder, so no test here follows the decoder if it
    moves."""
    assert abc_bms.PROTECTION_OFFSET == VENDOR_PROTECTION_OFFSET


def test_the_active_byte_is_the_one_the_pack_sends():
    """A state the pack sets reads as exactly this, and the driver has to
    agree with the pack rather than with itself.

    Worth its own check because every other test here builds its frames from
    the same constant it would be verifying. Change the driver's value and
    those tests change with it, agreeing happily while a pack reporting a
    real fault decodes as healthy.
    """
    assert abc_bms.PROTECTION_ACTIVE == VENDOR_PROTECTION_ACTIVE


def test_a_real_fault_byte_is_read_as_a_fault():
    """The end to end version of the check above, through parse rather than
    against a constant, so the two cannot both be wrong in the same way."""
    messages = fx.real_messages()
    frame = bytearray(fx.frame(abc_bms.ALARM_MESSAGE,
                               bytes(len(VENDOR_PROTECTION_ORDER))))
    frame[VENDOR_PROTECTION_OFFSET] = VENDOR_PROTECTION_ACTIVE
    messages[abc_bms.ALARM_MESSAGE] = bytes(frame)
    out = abc_bms.parse(messages)
    assert out["alarm_flags"] == 1.0
    assert out["alarm_active"] == VENDOR_PROTECTION_ORDER[0]


def test_the_protection_order_is_the_one_the_pack_sends():
    """A swapped pair here mislabels a fault, which is worse than not
    decoding one: somebody reads "pack too cold to charge" and goes out with
    a heater when a cell is actually under its voltage limit."""
    assert tuple(name for name, _ in abc_bms.PROTECTION_STATES) == (
        VENDOR_PROTECTION_ORDER)


def test_every_protection_state_says_what_it_means():
    """The short names are the vendor's and are opaque on their own. The
    descriptions are what makes the table readable to somebody who does not
    have the application in front of them."""
    for name, description in abc_bms.PROTECTION_STATES:
        assert name.isupper() and name.isalpha(), name
        assert len(description) > 10 and description[0].islower(), name


def test_a_contented_pack_publishes_a_zero_alarm_and_no_names():
    """Zero is the whole alerting rule, so it has to mean exactly one thing:
    every state was reported and none of them is set."""
    messages = fx.real_messages()
    messages[abc_bms.ALARM_MESSAGE] = protection_frame()
    out = abc_bms.parse(messages)
    assert out["alarm_flags"] == 0.0
    assert out["alarm_active"] == ""


def test_each_protection_state_owns_one_bit_in_the_order_reported():
    """The pack spends a whole byte on each state and this packs them into a
    number, so the number can only be taken apart if the order is fixed.

    Checked one at a time rather than in a lump, since a pair of swapped
    entries gives the same total and would read as correct.

    The position walked here is the pack's, not the decoder's. Taking it
    from the decoder's own table would mean a reordered table moved the
    expectation along with the thing it is meant to pin.
    """
    messages = fx.real_messages()
    for index, name in enumerate(VENDOR_PROTECTION_ORDER):
        messages[abc_bms.ALARM_MESSAGE] = protection_frame(name)
        out = abc_bms.parse(messages)
        assert out["alarm_flags"] == float(1 << index), name
        assert out["alarm_active"] == name


def test_several_states_at_once_are_all_named():
    """A pack in trouble usually trips more than one, and the first to be
    read is not the one that matters most."""
    messages = fx.real_messages()
    messages[abc_bms.ALARM_MESSAGE] = protection_frame("CUV", "OTD", "OVP")
    out = abc_bms.parse(messages)
    assert out["alarm_active"] == "CUV,OTD,OVP"
    assert out["alarm_flags"] > 0


def test_a_byte_that_is_not_the_active_value_is_not_an_alarm():
    """The vendor application tests for exactly one value, and so does this.
    Treating anything non-zero as a fault would invent alarms out of a byte
    the pack uses for something else, and an alarm nobody can act on is
    worse than none."""
    messages = fx.real_messages()
    frame = bytearray(protection_frame())
    frame[VENDOR_PROTECTION_OFFSET] = 0x20
    messages[abc_bms.ALARM_MESSAGE] = bytes(frame)
    out = abc_bms.parse(messages)
    assert out["alarm_flags"] == 0.0
    assert out["alarm_active"] == ""


def test_a_pack_that_said_nothing_publishes_no_alarm_at_all():
    """Absent is not the same as healthy. A calm zero for a pack that never
    answered is the one reading that could hide a fault, so the fields are
    left out and the staleness of the rest speaks for itself."""
    messages = fx.real_messages()
    messages.pop(abc_bms.ALARM_MESSAGE, None)
    out = abc_bms.parse(messages)
    assert "alarm_flags" not in out
    assert "alarm_active" not in out


def test_a_truncated_protection_reply_is_not_read_as_healthy():
    """Short by one state is short by one state. Reading what arrived and
    assuming the rest are clear would report a pack as content on the
    strength of bytes that never came."""
    messages = fx.real_messages()
    short = protection_frame()[:VENDOR_PROTECTION_OFFSET
                               + len(VENDOR_PROTECTION_ORDER) - 1]
    messages[abc_bms.ALARM_MESSAGE] = short
    out = abc_bms.parse(messages)
    assert "alarm_flags" not in out


def test_each_cell_is_published_separately():
    """The summary says the pack is fine. Only the per-cell series shows one
    cell walking away from the others over weeks."""
    out = abc_bms.parse(fx.real_messages())
    assert out["cell_01_volts"] == 3.304
    assert out["cell_16_volts"] == 3.292
    assert len([k for k in out if k.startswith("cell_") and k.endswith("_volts")
                and k[5:7].isdigit()]) == 16


def test_cell_field_names_sort_in_pack_order():
    """Zero padded so that cell 2 sorts before cell 10 in a dashboard."""
    out = abc_bms.parse(fx.real_messages())
    numbered = sorted(k for k in out if k.startswith("cell_") and k[5:7].isdigit())
    assert numbered[0] == "cell_01_volts" and numbered[1] == "cell_02_volts"
