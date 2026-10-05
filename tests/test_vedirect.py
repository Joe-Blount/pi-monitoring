import io

import pytest

from monitoring.drivers import vedirect
from monitoring.drivers.base import DriverError
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


# -- the production paths: a port that is there but says nothing -------------

class SilentPort:
    """A serial port that is open and returns a timeout on every read.

    What a connected cable with a dark controller looks like, or a by-id path
    that resolved to a different adapter.
    """

    def __init__(self):
        self.reads = 0

    def read(self, _n):
        self.reads += 1
        return b""

    def close(self):
        pass


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.5
        return self.now


def test_a_silent_port_gives_up_rather_than_waiting_forever():
    """Without a limit this blocks indefinitely: a check that never returns,
    an install script that stops with no message, and a long-lived child that
    emits nothing and never exits, so nothing restarts it."""
    with pytest.raises(DriverError) as exc:
        list(vedirect.frames(SilentPort(), idle_limit=5, clock=FakeClock()))
    assert "no complete frame" in str(exc.value)


def test_the_silent_port_message_names_the_two_likely_causes():
    with pytest.raises(DriverError) as exc:
        list(vedirect.frames(SilentPort(), idle_limit=1, clock=FakeClock()))
    message = str(exc.value)
    assert "controller may be off" in message and "port" in message


def test_the_driver_read_gives_up_on_a_silent_port():
    driver = vedirect.VedirectDriver("mppt", {"port": "/dev/fake",
                                              "frame_timeout": 1}, {})
    driver._serial = SilentPort()
    with pytest.raises(DriverError):
        driver.read()


def test_the_driver_check_gives_up_on_a_silent_port():
    driver = vedirect.VedirectDriver("mppt", {"port": "/dev/fake",
                                              "frame_timeout": 1}, {})
    driver._serial = SilentPort()
    with pytest.raises(DriverError):
        driver.check()


class NoisePort:
    """A stream that is not VE.Direct: the wrong baud rate, or a bad line."""

    def __init__(self, total):
        self.left = total

    def read(self, _n):
        if self.left <= 0:
            return b""
        self.left -= 1
        # Tabs and newlines are what make the parser build fields; without a
        # Checksum label those fields would accumulate forever.
        return b"\t" if self.left % 3 == 0 else b"\n" if self.left % 5 == 0 else b"z"

    def close(self):
        pass


def test_garbage_does_not_accumulate_without_limit():
    """Measured before the fix: 200 KB of noise produced no frames and left
    hundreds of fields holding over a hundred kilobytes, on a machine with a
    few hundred megabytes."""
    results = list(vedirect.frames(NoisePort(20000), stop_when_empty=True,
                                   max_frame_bytes=256))
    assert results, "noise must be reported as bad frames, not silently absorbed"
    assert all(ok is False for _, ok in results)
    # Each reported frame is bounded, so nothing grows without end.
    assert all(len(fields) < 200 for fields, _ in results)


def test_the_parser_recovers_after_abandoning_an_oversized_frame():
    """Recovery costs one frame, for the same reason attaching mid-stream
    does: the garbage before the abandon is counted into the checksum of
    whatever follows it. The frame after that is clean."""
    data = b"z" * 2000 + fx.frame(fx.SUNNY) + fx.frame(fx.NIGHT)
    results = list(vedirect.frames(io.BytesIO(data), stop_when_empty=True,
                                   max_frame_bytes=256))
    assert results[-1][1] is True, "the parser must recover, not stay broken"


# -- stream thinning ---------------------------------------------------------

class ReplayPort:
    def __init__(self, data):
        self.data = io.BytesIO(data)

    def read(self, n):
        return self.data.read(n)

    def close(self):
        pass


def drain(driver):
    """Collect what a stream emits before the port falls silent."""
    out = []
    try:
        for reading in driver.stream():
            out.append(reading)
    except DriverError:
        pass            # the finite test port runs out, which is the giving-up path
    return out


def streaming_driver(frames_of_data, emit_every="30s"):
    driver = vedirect.VedirectDriver(
        "mppt", {"port": "/dev/fake", "emit_every": emit_every,
                 "frame_timeout": 0.01}, {})
    driver._serial = ReplayPort(frames_of_data)
    return driver


def test_streaming_publishes_at_the_asked_rate_not_once_per_frame(monkeypatch):
    """Frames arrive about once a second. Publishing all of them would store
    thirty times the data for no extra information."""
    monkeypatch.setattr(vedirect.time, "time", lambda: 1000.0)
    emitted = drain(streaming_driver(fx.frame(fx.SUNNY) * 10))
    assert len(emitted) == 1, "ten frames in one window must publish once"


def test_streaming_emits_again_once_the_window_has_passed(monkeypatch):
    clock = [1000.0]

    def advancing():
        clock[0] += 20.0
        return clock[0]

    monkeypatch.setattr(vedirect.time, "time", advancing)
    emitted = drain(streaming_driver(fx.frame(fx.SUNNY) * 10))
    assert len(emitted) > 1


# -- the driver's own production paths ---------------------------------------

def frame_source(data):
    driver = vedirect.VedirectDriver("mppt", {"port": "/dev/fake",
                                              "frame_timeout": 0.01}, {})
    driver._serial = ReplayPort(data)
    return driver


def test_read_returns_the_first_good_frame():
    """The method telegraf actually calls. Only the parser underneath it had
    a test."""
    values = frame_source(fx.frame(fx.SUNNY)).read()
    assert values["battery_volts"] == 13.51
    assert values["charge_state"] == "bulk"


def test_read_skips_a_corrupt_frame_and_uses_the_next():
    """A noisy line is normal; the controller simply sends another."""
    values = frame_source(fx.corrupt(fx.frame(fx.SUNNY)) + fx.frame(fx.NIGHT)).read()
    assert values["pv_watts"] == 0.0


def test_read_gives_up_when_every_frame_is_corrupt():
    data = fx.corrupt(fx.frame(fx.SUNNY)) * 5
    with pytest.raises(DriverError):
        frame_source(data).read()


def test_check_reports_the_controller_identity():
    """What the install script prints while someone is standing at the
    machine, so it has to name the device rather than just say yes."""
    detail = frame_source(fx.frame(fx.SUNNY)).check()
    assert "HQ25453MGTX" in detail and "0xA057" in detail


class EndlessCorruptPort:
    """A line that keeps talking and never says anything valid.

    Distinct from a silent port: here bytes do arrive, so the idle limit never
    fires and the frame deadline is what has to stop it.
    """

    def __init__(self):
        self.data = fx.corrupt(fx.frame(fx.SUNNY))
        self.at = 0

    def read(self, n):
        out = self.data[self.at:self.at + n]
        self.at = (self.at + n) % len(self.data)
        return out

    def close(self):
        pass


def test_check_fails_clearly_when_a_talkative_line_says_nothing_valid():
    driver = vedirect.VedirectDriver("mppt", {"port": "/dev/fake",
                                              "frame_timeout": 0.2}, {})
    driver._serial = EndlessCorruptPort()
    with pytest.raises(DriverError) as exc:
        driver.check()
    assert "none valid" in str(exc.value)
    assert "nothing else holds the port" in str(exc.value)


def test_raw_returns_the_unparsed_fields_for_bringing_up_a_cable():
    out = frame_source(fx.frame(fx.SUNNY)).raw()
    assert out["checksum_ok"] is True
    assert out["fields"]["V"] == "13510"


def test_a_long_run_of_corrupt_frames_ends_the_stream():
    """A persistently bad line must exit so telegraf restarts the child,
    rather than streaming silence forever."""
    driver = frame_source(fx.corrupt(fx.frame(fx.SUNNY)) * 120)
    with pytest.raises(DriverError) as exc:
        list(driver.stream())
    assert "consecutive frames failed" in str(exc.value)


def test_an_off_reason_that_is_not_hex_does_not_lose_the_frame():
    values = vedirect.normalize(dict(fx.SUNNY, OR="unexpected"))
    assert values["off_reason"] == "unexpected"
    assert "off_reason_code" not in values
    assert values["battery_volts"] == 13.51
