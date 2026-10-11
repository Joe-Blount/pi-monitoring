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
    """Asserted against the bytes the exchange actually writes.

    Checking what a replaced transport was asked for proves nothing: the
    request encoding and every write live below that seam. This drives the
    real exchange against a stand-in radio and reads the wire.
    """
    client = radio()
    driver = build(transport=transport_using(client))
    driver.read()
    driver.raw()
    assert client.writes, "the exchange wrote nothing, so this proves nothing"
    for written in client.writes:
        assert len(written) == 2
        assert written[0] not in WRITE_COMMANDS
        assert written[0] in qinglan.FRAME_LENGTH
        assert written[1] == fx.REAL_ADDRESS


# -- the exchange, driven against a stand-in radio -------------------------

def radio(**kwargs):
    kwargs.setdefault("replies", {
        qinglan.LIVE: fx.frame(fx.REAL_LIVE_SERIES[0]),
        qinglan.SETTINGS: fx.frame(fx.REAL_SETTINGS),
        qinglan.ACCUMULATED: fx.frame(fx.REAL_ACCUMULATED),
        qinglan.PASSWORD: fx.frame(fx.REAL_PASSWORD),
    })
    return fx.FakeClient("C8:47:80:52:16:A9", 1.0, **kwargs)


def transport_using(client, timeout=1.0, settle=0.01):
    return qinglan._BleakTransport(
        "C8:47:80:52:16:A9", fx.REAL_ADDRESS, timeout, settle, client)


def test_the_exchange_writes_the_command_and_the_bus_address():
    client = radio()
    transport_using(client).exchange((qinglan.LIVE,))
    assert client.writes == [b"\xfb\x01"]


def test_a_record_split_across_many_notifications_is_rejoined():
    """The longest record does not fit one packet at the negotiated size."""
    client = radio(chunk=7)
    messages = transport_using(client).exchange((qinglan.ACCUMULATED,))
    assert messages[qinglan.ACCUMULATED] == fx.frame(fx.REAL_ACCUMULATED)


def test_a_reply_that_arrives_too_late_does_not_spoil_the_next_record():
    """A record is found by its own command byte, not by what came first.

    The delay here is longer than the gaps allowed, so the first command is
    given up on and its reply lands inside the second command's window. The
    second record must still be read.
    """
    # The first reply is held back until the second command is written, so
    # it is in the buffer when the second record is read. Ordered by what
    # the exchange does rather than by the clock, so a busy machine cannot
    # turn this into a different test.
    client = radio(hold={qinglan.LIVE: qinglan.SETTINGS})
    messages = transport_using(client, timeout=5.0, settle=0.02).exchange(
        (qinglan.LIVE, qinglan.SETTINGS))
    assert messages[qinglan.SETTINGS] == fx.frame(fx.REAL_SETTINGS)
    assert qinglan.LIVE not in messages
    assert messages[qinglan.UNANSWERED] == [qinglan.LIVE]


def test_a_command_byte_inside_a_stale_payload_is_not_mistaken_for_a_reply():
    """A command byte is an ordinary value that also occurs in payloads.

    A current of 2.54 A is 0x00FE, and 0xFE is the settings command. The
    tail of an earlier reply therefore offers a false start, and locking on
    to it would consume part of the real reply and lose both.
    """
    decoy = fx.live_frame(amps=2.54)
    assert bytes(decoy).count(qinglan.SETTINGS) > 0, "this test needs a decoy"
    client = radio(replies={qinglan.LIVE: decoy,
                            qinglan.SETTINGS: fx.frame(fx.REAL_SETTINGS)},
                   hold={qinglan.LIVE: qinglan.SETTINGS})
    messages = transport_using(client, timeout=5.0, settle=0.02).exchange(
        (qinglan.LIVE, qinglan.SETTINGS))
    assert messages[qinglan.SETTINGS] == fx.frame(fx.REAL_SETTINGS)


def test_a_decoy_that_passes_framing_is_refused_by_the_parser():
    """Framing cannot be perfect without a checksum, so parsing is the guard.

    A payload holding the settings command followed by the device address
    cannot be told from a settings reply. What must never happen is a wrong
    number reaching the database, and that is the parser's job.
    """
    decoy = fx.live_frame(amps=650.25)          # 65025 is 0xFE01
    assert bytes(decoy).find(bytes([qinglan.SETTINGS, fx.REAL_ADDRESS])) > 0
    client = radio(replies={qinglan.LIVE: decoy,
                            qinglan.SETTINGS: fx.frame(fx.REAL_SETTINGS)},
                   hold={qinglan.LIVE: qinglan.SETTINGS})
    messages = transport_using(client, timeout=5.0, settle=0.02).exchange(
        (qinglan.LIVE, qinglan.SETTINGS))
    settings = messages.get(qinglan.SETTINGS)
    if settings == fx.frame(fx.REAL_SETTINGS):
        return                                   # framing stepped past it
    # Framing was fooled, so nothing wrong may be published.
    with pytest.raises(DriverError):
        qinglan.parse({qinglan.LIVE: fx.frame(fx.REAL_LIVE_SERIES[0]),
                       qinglan.SETTINGS: settings})


def test_a_reply_from_another_address_is_stepped_past_not_locked_onto():
    stray = fx.live_frame(address=5)
    client = radio(replies={qinglan.LIVE: stray,
                            qinglan.SETTINGS: fx.frame(fx.REAL_SETTINGS)},
                   hold={qinglan.LIVE: qinglan.SETTINGS})
    messages = transport_using(client, timeout=5.0, settle=0.02).exchange(
        (qinglan.LIVE, qinglan.SETTINGS))
    assert messages[qinglan.SETTINGS] == fx.frame(fx.REAL_SETTINGS)


def test_traffic_that_answers_nothing_is_stepped_over():
    client = radio(noise=b"\x11\x22\x33\x44")
    messages = transport_using(client).exchange((qinglan.LIVE,))
    assert messages[qinglan.LIVE] == fx.frame(fx.REAL_LIVE_SERIES[0])


def test_a_device_that_chatters_cannot_grow_the_reader_without_limit():
    """Memory is the scarce thing on the machine this runs on."""
    chatter = 100000
    client = radio(replies={qinglan.LIVE: b"\x11" * chatter}, chunk=1000)
    transport = transport_using(client)
    with pytest.raises(DriverError):
        transport.exchange((qinglan.LIVE,))
    assert transport.peak_buffer <= qinglan.MAX_BUFFER_BYTES
    assert transport.peak_buffer < chatter
    assert transport.overflowed > 0


def test_a_record_that_starts_after_a_quiet_gap_is_still_gathered():
    """A device slower than one gap must not be abandoned before it speaks."""
    client = radio(delay=0.08)
    messages = transport_using(client, settle=0.05).exchange((qinglan.LIVE,))
    assert messages[qinglan.LIVE] == fx.frame(fx.REAL_LIVE_SERIES[0])


def test_a_record_dribbling_in_with_gaps_between_pieces_is_gathered():
    """Each piece that arrives is progress, so the waiting starts over.

    Counting gaps without resetting them abandons a record that is still
    arriving, which loses a reading that was most of the way there.
    """
    client = radio(chunk=6, chunk_delay=0.08)
    messages = transport_using(client, settle=0.05).exchange((qinglan.LIVE,))
    assert messages[qinglan.LIVE] == fx.frame(fx.REAL_LIVE_SERIES[0])


def test_a_silent_device_gives_up_rather_than_waiting_for_ever():
    client = radio(silent=True)
    with pytest.raises(DriverError) as raised:
        transport_using(client, timeout=2.0).exchange((qinglan.LIVE,))
    assert "answered nothing" in str(raised.value)


def test_a_radio_that_never_connects_is_abandoned_at_the_deadline():
    """The installer runs a check with telegraf stopped and no timeout.

    A stall there leaves the node silent until somebody reaches it, so the
    deadline has to live in the driver. The call is watched from another
    thread, so that losing the deadline fails this test rather than hanging
    it: a test that hangs stops the suite instead of reporting.
    """
    import threading
    outcome = []

    def attempt():
        try:
            transport_using(radio(hang=True), timeout=0.3).exchange(
                (qinglan.LIVE,))
            outcome.append(None)
        except DriverError as exc:
            outcome.append(exc)

    watcher = threading.Thread(target=attempt, daemon=True)
    watcher.start()
    watcher.join(timeout=10.0)

    assert not watcher.is_alive(), "the exchange ran past its own deadline"
    assert outcome and outcome[0] is not None
    assert "abandoned" in str(outcome[0])


def test_a_radio_that_stalls_even_while_disconnecting_is_let_go(monkeypatch):
    """The deadline alone is not enough, because cancelling can hang too.

    Abandoning a coroutine waits for its cleanup, and closing a Bluetooth
    connection waits on the system bus, which is the thing already wedged
    when the deadline fires. The caller must not wait on that.
    """
    import threading
    import time
    monkeypatch.setattr(qinglan, "JOIN_GRACE_SECONDS", 0.3)
    outcome = []

    def attempt():
        client = radio(hang_on_write=True, hang_on_exit=True)
        try:
            transport_using(client, timeout=0.3).exchange((qinglan.LIVE,))
            outcome.append(None)
        except DriverError as exc:
            outcome.append(exc)

    started = time.monotonic()
    watcher = threading.Thread(target=attempt, daemon=True)
    watcher.start()
    watcher.join(timeout=10.0)

    assert not watcher.is_alive(), "a stalled teardown held the caller"
    assert time.monotonic() - started < 5.0
    assert outcome and outcome[0] is not None
    assert "abandoned" in str(outcome[0])


def test_one_command_answering_and_one_not_is_reported_not_hidden():
    client = radio(replies={qinglan.LIVE: fx.frame(fx.REAL_LIVE_SERIES[0])})
    messages = transport_using(client).exchange(
        (qinglan.LIVE, qinglan.SETTINGS))
    assert messages[qinglan.UNANSWERED] == [qinglan.SETTINGS]
