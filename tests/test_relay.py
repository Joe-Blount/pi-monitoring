"""Relay polarity, asserted as pin levels rather than as intentions.

Every test here checks the level driven on the pin, never the decision that
led to it. A test phrased as "the controller connected the cameras" passes
just as happily while the pin drives the opposite way and the cameras run all
night and are dark all day. There is no reading that reveals that and no
alarm that fires, so the only defence is to assert the voltage.
"""

import pytest

from monitoring.control import relay


class Recorder:
    """Stands in for a gpiozero output device and remembers the levels."""

    def __init__(self, pin):
        self.pin = pin
        self.levels = []
        self.closed = False

    def on(self):
        self.levels.append(True)

    def off(self):
        self.levels.append(False)

    def close(self):
        self.closed = True


# -- the arithmetic ---------------------------------------------------------

def test_a_shed_to_energize_load_runs_when_the_pin_is_low():
    """The cameras and the recorder are wired this way, so a dead Pi leaves
    the site watched and a reboot does not power-cycle a recorder.

    Written as the literal pair rather than through the constant, because
    comparing level_for's output against level_for's own notion of polarity
    would hold whatever the function did.
    """
    assert relay.level_for("energize_to_shed", True) is False
    assert relay.level_for("energize_to_shed", False) is True


def test_a_connect_to_energize_load_runs_when_the_pin_is_high():
    """Irrigation is wired this way. A pump running unattended because a Pi
    rebooted is a different kind of problem from a dark camera."""
    assert relay.level_for("energize_to_connect", True) is True
    assert relay.level_for("energize_to_connect", False) is False


def test_the_two_polarities_are_opposites():
    """Not a tautology: it fails if both names map to the same arithmetic,
    which is what a copy-paste edit of this module would produce."""
    for connected in (True, False):
        assert (relay.level_for("energize_to_shed", connected)
                is not relay.level_for("energize_to_connect", connected))


def test_an_unknown_polarity_is_refused_rather_than_guessed():
    """A default here would decide what a dead controller leaves behind, for
    a load whose node file never said."""
    with pytest.raises(relay.RelayError) as raised:
        relay.level_for("whatever", True)
    assert "dead controller" in str(raised.value)

    with pytest.raises(relay.RelayError):
        relay.Relay(23, "backwards")


# -- driving a pin ----------------------------------------------------------

def test_the_level_reaches_the_pin_for_a_shed_to_energize_load():
    recorded = []
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED,
                      factory=lambda number: recorded.append(
                          Recorder(number)) or recorded[-1])
    pin.apply(True)
    pin.apply(False)
    assert recorded[0].levels == [False, True], (
        "connected must drive the pin low on this polarity")


def test_the_level_reaches_the_pin_for_a_connect_to_energize_load():
    recorded = []
    pin = relay.Relay(25, relay.ENERGIZE_TO_CONNECT,
                      factory=lambda number: recorded.append(
                          Recorder(number)) or recorded[-1])
    pin.apply(True)
    pin.apply(False)
    assert recorded[0].levels == [True, False]


def test_the_pin_number_asked_for_is_the_pin_opened():
    """Off by one here drives a neighbouring load."""
    seen = []
    pin = relay.Relay(22, relay.ENERGIZE_TO_CONNECT,
                      factory=lambda number: seen.append(number) or Recorder(number))
    pin.apply(True)
    assert seen == [22]


def test_the_pin_is_opened_once_and_reused():
    """Reopening a line on every pass would release and reclaim it, and a
    load wired to the normally closed contact would twitch each time."""
    opened = []
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED,
                      factory=lambda number: opened.append(number) or Recorder(number))
    for _ in range(5):
        pin.apply(True)
    assert len(opened) == 1


def test_apply_reports_the_level_it_drove():
    """The loop publishes this, so it must be what happened rather than what
    was wanted."""
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED,
                      factory=Recorder)
    assert pin.apply(True) is False
    assert pin.apply(False) is True


def test_closing_releases_the_line():
    device = Recorder(23)
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, factory=lambda _n: device)
    pin.apply(True)
    pin.close()
    assert device.closed
    pin.close(), "closing twice must not raise"


def test_a_pin_that_cannot_be_driven_names_itself():
    def explode(_number):
        raise OSError("line is busy")

    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, factory=explode)
    with pytest.raises(OSError):
        pin.apply(True)


# -- the dry run ------------------------------------------------------------

def test_a_pretend_relay_computes_the_same_levels_as_a_real_one():
    """It subclasses rather than reimplements, so the arithmetic under test
    is the arithmetic that ships. A separate implementation would be free to
    agree with the tests and disagree with the hardware."""
    for polarity in relay.POLARITIES:
        pretend = relay.Pretend(23, polarity)
        for connected in (True, False, True):
            assert pretend.apply(connected) == relay.level_for(polarity,
                                                               connected)
        assert pretend.levels == [relay.level_for(polarity, True),
                                  relay.level_for(polarity, False),
                                  relay.level_for(polarity, True)]


def test_a_pretend_relay_touches_no_hardware():
    """A dry run on a machine with the real pin in use must not claim it."""
    pretend = relay.Pretend(23, relay.ENERGIZE_TO_SHED)
    pretend.apply(True)
    assert pretend._device is None


# -- the backend that releases a pin ----------------------------------------

def test_the_pin_backend_is_the_one_that_releases_on_death():
    """The fail-safe rests on a dying process releasing its pins. The sysfs
    backend does not, and leaves the line driven and busy so the next start
    cannot claim it. Named here rather than left to whatever the environment
    happens to carry."""
    assert relay.PIN_FACTORY == "lgpio"


# -- the real backend, without hardware -------------------------------------

def _stub_gpiozero(monkeypatch, calls):
    """Put a fake gpiozero in front of the import inside _open.

    The import is function-local, so replacing the module entry is enough and
    no hardware is touched. This is the only way to assert how the device is
    constructed, and how it is constructed is the difference between a clean
    claim and a recorder that twitches every time the controller starts.
    """
    import sys
    import types

    module = types.ModuleType("gpiozero")

    class Device:
        def __init__(self, pin, **kwargs):
            calls.append((pin, kwargs))
            self.levels = []

        def on(self):
            self.levels.append(True)

        def off(self):
            self.levels.append(False)

        def close(self):
            pass

    module.DigitalOutputDevice = Device
    monkeypatch.setitem(sys.modules, "gpiozero", module)


def test_claiming_a_pin_does_not_drive_it_high(monkeypatch):
    """A pin claimed high would energize the relay for the width of the call.

    On the cameras, wired so that energizing sheds, that is the recorder cut
    every time the controller starts, which is the thing the normally closed
    wiring exists to prevent. Asserted on the constructor because there is no
    way to observe a pulse that short from outside.
    """
    calls = []
    _stub_gpiozero(monkeypatch, calls)
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED)
    pin.apply(True)
    assert calls, "the device was never constructed"
    number, kwargs = calls[0]
    assert number == 23
    assert kwargs.get("initial_value") is False, (
        "the pin was claimed in a state that energizes the relay")


def test_the_backend_is_requested_before_gpiozero_is_imported(monkeypatch):
    """gpiozero reads the backend at import time, so setting it afterwards is
    ignored and the fallback is the one that never releases a pin."""
    import os as _os

    monkeypatch.delenv("GPIOZERO_PIN_FACTORY", raising=False)
    _stub_gpiozero(monkeypatch, [])
    relay.Relay(23, relay.ENERGIZE_TO_SHED).apply(True)
    assert _os.environ.get("GPIOZERO_PIN_FACTORY") == relay.PIN_FACTORY


def test_a_missing_gpiozero_says_so_rather_than_failing_obscurely(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "gpiozero", None)
    with pytest.raises(relay.RelayError) as raised:
        relay.Relay(23, relay.ENERGIZE_TO_SHED).apply(True)
    assert "gpiozero" in str(raised.value)
