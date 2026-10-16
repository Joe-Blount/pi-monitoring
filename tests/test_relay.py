"""Relay polarity, asserted as pin voltage rather than as intention.

Three places polarity can invert, and a test has to be able to see all of
them:

1. The arithmetic turning a wanted state into a level.
2. The branch turning a level into a call on the device.
3. The library's own ``active_high``, which inverts the state the pin is
   claimed in as well as every level after it.

The third is the one that matters most and is the hardest to see, because
``on()`` and ``off()`` are logical rather than electrical: with
``active_high`` wrong, ``on()`` drives the pin low. So the tests that care
about voltage use gpiozero's mock factory and read ``device.pin.state``,
which is the voltage. A double that records ``on`` and ``off`` calls cannot
tell the difference, and a test phrased as "the controller connected the
cameras" passes while the cameras run all night and are dark all day.
"""

import pytest

from monitoring.control import relay


@pytest.fixture
def pins():
    """gpiozero's mock factory, so a test can read the pin's voltage."""
    import os
    os.environ["GPIOZERO_PIN_FACTORY"] = "mock"
    from gpiozero import Device
    from gpiozero.pins.mock import MockFactory

    previous = Device.pin_factory
    Device.pin_factory = MockFactory()
    yield Device.pin_factory
    Device.pin_factory.reset()
    Device.pin_factory = previous


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


# -- the voltage on the pin -------------------------------------------------

@pytest.mark.parametrize("polarity,connected,expected", [
    ("energize_to_shed", True, False),
    ("energize_to_shed", False, True),
    ("energize_to_connect", True, True),
    ("energize_to_connect", False, False),
])
def test_the_pin_carries_the_voltage_the_polarity_demands(pins, polarity,
                                                          connected, expected):
    """Through the real library, reading the pin rather than the call.

    This is the only test that can see an inverted `active_high`, because
    with that wrong `on()` drives the pin low and a double recording `on`
    and `off` sees nothing amiss.
    """
    pin = relay.Relay(23, polarity)
    pin.apply(connected)
    assert pin._device.pin.state is expected
    pin.close()


def test_claiming_a_pin_leaves_it_low_whatever_the_load(pins):
    """A pin claimed high energizes the relay for the width of the call. On
    the cameras that is the recorder cut every time the controller starts,
    which is what the normally closed wiring exists to prevent."""
    for polarity in relay.POLARITIES:
        pin = relay.Relay(24, polarity)
        pin._open()
        assert pin._device.pin.state is False, polarity
        pin.close()


def test_the_backend_is_requested_rather_than_inherited(monkeypatch):
    """The sysfs fallback does not release a pin when the process holding it
    dies, which is the property every fail-safe here depends on. An
    environment already naming it must not win."""
    import os
    monkeypatch.setenv("GPIOZERO_PIN_FACTORY", "something-else")
    try:
        relay.open_output(23)
    except relay.RelayError:
        pass
    assert os.environ["GPIOZERO_PIN_FACTORY"] == relay.PIN_FACTORY


def test_a_pin_that_cannot_be_claimed_names_itself_and_the_pin(pins):
    """The message is read by somebody standing at a machine."""
    taken = relay.Relay(25, relay.ENERGIZE_TO_SHED)
    taken._open()
    second = relay.Relay(25, relay.ENERGIZE_TO_SHED)
    with pytest.raises(relay.RelayError) as raised:
        second._open()
    assert "GPIO25" in str(raised.value)
    taken.close()


# -- driving, through the recording device ----------------------------------

def test_the_level_reaches_the_device_for_each_polarity():
    shed = relay.Relay(23, relay.ENERGIZE_TO_SHED, factory=relay.Recording)
    shed.apply(True)
    shed.apply(False)
    assert shed._device.levels == [False, True]

    connect = relay.Relay(25, relay.ENERGIZE_TO_CONNECT,
                          factory=relay.Recording)
    connect.apply(True)
    connect.apply(False)
    assert connect._device.levels == [True, False]


def test_the_device_is_constructed_the_way_the_shipping_code_constructs_it():
    """The double and the real device take the same call.

    A double with a convenience signature of its own would mean every test
    here exercised a construction the shipping code never makes, and the
    construction is where two of the three polarity inversions live.
    """
    pin = relay.Relay(22, relay.ENERGIZE_TO_CONNECT, factory=relay.Recording)
    pin._open()
    assert pin._device.pin == 22
    assert pin._device.claimed is False
    assert pin._device.active_high is True


def test_the_pin_is_opened_once_and_reused():
    """Reopening a line on every pass would release and reclaim it, and a
    load wired to the normally closed contact would twitch each time."""
    opened = []

    def counting(pin, initial_value=False):
        opened.append(pin)
        return relay.Recording(pin, initial_value=initial_value)

    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, factory=counting)
    for _ in range(5):
        pin.apply(True)
    assert len(opened) == 1


def test_apply_reports_the_level_it_drove():
    """The loop publishes this, so it must be what happened rather than what
    was wanted."""
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, factory=relay.Recording)
    assert pin.apply(True) is False
    assert pin.apply(False) is True


def test_closing_releases_the_line_and_can_be_repeated():
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, factory=relay.Recording)
    pin.apply(True)
    device = pin._device
    pin.close()
    assert device.closed
    pin.close()


def test_a_failed_claim_leaves_no_device_and_is_retried():
    attempts = []

    def flaky(pin, initial_value=False):
        attempts.append(pin)
        if len(attempts) == 1:
            raise relay.RelayError("line is busy")
        return relay.Recording(pin, initial_value=initial_value)

    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, factory=flaky)
    with pytest.raises(relay.RelayError):
        pin.apply(True)
    assert pin._device is None, "a failed claim left a half-open device"
    assert pin.apply(True) is False


# -- the dry run ------------------------------------------------------------

def test_a_dry_run_goes_through_the_same_apply():
    """It swaps the device, not the logic. Overriding apply would leave the
    branch that turns a level into a call unreachable from a dry run, so a
    dry run on site would show the right answer while the real relay drove
    the wrong way."""
    for polarity in relay.POLARITIES:
        pretend = relay.Pretend(23, polarity)
        for connected in (True, False, True):
            pretend.apply(connected)
        assert pretend.levels == [relay.level_for(polarity, True),
                                  relay.level_for(polarity, False),
                                  relay.level_for(polarity, True)]


def test_a_dry_run_records_through_the_device_not_beside_it(pins):
    """The levels a dry run reports must come from the device the shared
    apply drove, not from a list a reimplemented apply kept.

    Without this, overriding apply again passes every other test here: the
    levels come out identical, and the branch turning a level into a call
    goes unexercised, which is the one thing the dry run is meant to
    rehearse.
    """
    pretend = relay.Pretend(23, relay.ENERGIZE_TO_SHED)
    pretend.apply(True)
    assert pretend._device is not None, (
        "apply did not go through the shared open and drive path")
    assert pretend.levels is pretend._device.levels


def test_a_dry_run_claims_no_pin(pins):
    """On a machine where the controller already holds the line, a dry run
    must not try to take it."""
    taken = relay.Relay(23, relay.ENERGIZE_TO_SHED)
    taken._open()
    pretend = relay.Pretend(23, relay.ENERGIZE_TO_SHED)
    pretend.apply(True)
    assert pretend.levels == [False]
    taken.close()


def test_a_dry_run_reports_no_levels_before_it_is_used():
    assert relay.Pretend(23, relay.ENERGIZE_TO_SHED).levels == []


# -- the backend that releases a pin ----------------------------------------

def test_the_pin_backend_is_the_one_that_releases_on_death():
    """The fail-safe rests on a dying process releasing its pins. The sysfs
    backend does not, and leaves the line driven and busy so the next start
    cannot claim it."""
    assert relay.PIN_FACTORY == "lgpio"


def test_a_missing_gpiozero_says_so_rather_than_failing_obscurely(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "gpiozero", None)
    with pytest.raises(relay.RelayError) as raised:
        relay.open_output(23)
    assert "gpiozero" in str(raised.value)
