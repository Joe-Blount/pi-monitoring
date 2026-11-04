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
def pins(monkeypatch):
    """gpiozero's mock factory, so a test can read the pin's voltage, put
    where the controller takes its factory from."""
    from gpiozero.pins.mock import MockFactory

    factory = MockFactory()
    monkeypatch.setattr(relay, "controller_factory", lambda: factory)
    yield factory
    factory.reset()


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
        relay.Relay(23, "backwards", relay.HIGH)


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
    pin = relay.Relay(23, polarity, relay.HIGH)
    pin.apply(connected)
    assert pin._device.pin.state is expected
    pin.close()


def test_claiming_a_pin_leaves_it_low_whatever_the_load(pins):
    """A pin claimed high energizes the relay for the width of the call. On
    the cameras that is the recorder cut every time the controller starts,
    which is what the normally closed wiring exists to prevent."""
    for polarity in relay.POLARITIES:
        pin = relay.Relay(24, polarity, relay.HIGH)
        pin._open()
        assert pin._device.pin.state is False, polarity
        pin.close()




def test_a_pin_that_cannot_be_claimed_names_itself_and_the_pin(pins):
    """The message is read by somebody standing at a machine."""
    taken = relay.Relay(25, relay.ENERGIZE_TO_SHED, relay.HIGH)
    taken._open()
    second = relay.Relay(25, relay.ENERGIZE_TO_SHED, relay.HIGH)
    with pytest.raises(relay.RelayError) as raised:
        second._open()
    assert "GPIO25" in str(raised.value)
    taken.close()


# -- driving, through the recording device ----------------------------------

def test_the_level_reaches_the_device_for_each_polarity():
    shed = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.HIGH, factory=relay.Recording)
    shed.apply(True)
    shed.apply(False)
    assert shed._device.levels == [False, True]

    connect = relay.Relay(25, relay.ENERGIZE_TO_CONNECT, relay.HIGH,
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
    pin = relay.Relay(22, relay.ENERGIZE_TO_CONNECT, relay.HIGH, factory=relay.Recording)
    pin._open()
    assert pin._device.pin == 22
    assert pin._device.claimed is False
    assert pin._device.active_high is True


def test_the_pin_is_opened_once_and_reused():
    """Reopening a line on every pass would release and reclaim it, and a
    load wired to the normally closed contact would twitch each time."""
    opened = []

    def counting(pin, initial_value=False, active_high=True):
        opened.append(pin)
        return relay.Recording(pin, initial_value=initial_value)

    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.HIGH, factory=counting)
    for _ in range(5):
        pin.apply(True)
    assert len(opened) == 1


def test_apply_reports_the_level_it_drove():
    """The loop publishes this, so it must be what happened rather than what
    was wanted."""
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.HIGH, factory=relay.Recording)
    assert pin.apply(True) is False
    assert pin.apply(False) is True


def test_closing_releases_the_line_and_can_be_repeated():
    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.HIGH, factory=relay.Recording)
    pin.apply(True)
    device = pin._device
    assert not device.closed, "the line was released while still driven"
    pin.close()
    assert device.closed
    pin.close()


def test_a_closed_relay_claims_the_line_afresh_when_driven_again():
    """Once closed, the device is gone. Driving the relay again has to claim
    the line again rather than call a device that no longer holds it, which
    on the real backend is a call on a released handle."""
    opened = []

    def counting(pin, initial_value=False, active_high=True):
        device = relay.Recording(pin, initial_value=initial_value)
        opened.append(device)
        return device

    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.HIGH, factory=counting)
    pin.apply(True)
    pin.close()
    assert pin.apply(True) is False
    assert len(opened) == 2
    assert opened[0].closed and not opened[1].closed
    assert opened[1].levels == [False]


def test_a_failed_claim_leaves_no_device_and_is_retried():
    attempts = []

    def flaky(pin, initial_value=False, active_high=True):
        attempts.append(pin)
        if len(attempts) == 1:
            raise relay.RelayError("line is busy")
        return relay.Recording(pin, initial_value=initial_value)

    pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.HIGH, factory=flaky)
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
        pretend = relay.Pretend(23, polarity, relay.HIGH)
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
    pretend = relay.Pretend(23, relay.ENERGIZE_TO_SHED, relay.HIGH)
    pretend.apply(True)
    assert pretend._device is not None, (
        "apply did not go through the shared open and drive path")
    assert pretend.levels is pretend._device.levels


def test_a_dry_run_claims_no_pin(pins):
    """On a machine where the controller already holds the line, a dry run
    must not try to take it."""
    taken = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.HIGH)
    taken._open()
    pretend = relay.Pretend(23, relay.ENERGIZE_TO_SHED, relay.HIGH)
    pretend.apply(True)
    assert pretend.levels == [False]
    taken.close()


def test_a_dry_run_reports_no_levels_before_it_is_used():
    assert relay.Pretend(23, relay.ENERGIZE_TO_SHED, relay.HIGH).levels == []


# -- the backend that releases a pin ----------------------------------------

def test_the_pin_backend_is_the_one_that_releases_on_death(lgpio):
    """The fail-safe rests on a dying process releasing its pins. The sysfs
    backend does not, and leaves the line driven and busy so the next start
    cannot claim it."""
    from gpiozero.pins.lgpio import LGPIOFactory
    factory_class, _ = relay.lgpio_classes()
    assert issubclass(factory_class, LGPIOFactory)


def test_a_missing_gpiozero_says_so_rather_than_failing_obscurely(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "gpiozero", None)
    with pytest.raises(relay.RelayError) as raised:
        relay.open_output(23)
    assert "gpiozero" in str(raised.value)


# -- a kernel that keeps outputs driven -------------------------------------

#: The setting's real location, read before any test points it elsewhere.
KERNEL_SETTING = relay.PERSIST_PARAMETER


def persist(tmp_path, monkeypatch, value):
    path = tmp_path / "persist_gpio_outputs"
    if value is not None:
        path.write_text(value + "\n")
    monkeypatch.setattr(relay, "PERSIST_PARAMETER", str(path))


def test_no_pin_is_claimed_while_the_kernel_keeps_outputs_after_death(
        tmp_path, monkeypatch):
    """On such a kernel a killed controller leaves its relays where they
    were, and an inverter or a pump stays energized. Refusing every claim
    leaves each load at its wired fail-safe instead. Even a claim released
    at once drives the pin for its width, so none may be made at all."""
    import sys
    import types
    claims = []
    module = types.ModuleType("gpiozero")
    module.DigitalOutputDevice = lambda *args, **kwargs: claims.append(args)
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    persist(tmp_path, monkeypatch, "Y")
    with pytest.raises(relay.RelayError) as raised:
        relay.open_output(25)
    message = str(raised.value)
    assert "GPIO25" in message
    assert relay.PERSIST_FIX in message
    assert "cmdline.txt" in message
    assert claims == []


@pytest.mark.parametrize("value", ["N", None])
def test_a_kernel_that_releases_outputs_is_driven(tmp_path, monkeypatch, pins,
                                                  value):
    """Older kernels have no such setting at all, and that is not a refusal."""
    persist(tmp_path, monkeypatch, value)
    device = relay.open_output(25)
    device.close()


def test_the_fix_names_the_kernel_setting_it_turns_off():
    assert relay.PERSIST_FIX == "pinctrl_bcm2835.persist_gpio_outputs=n"
    assert KERNEL_SETTING == (
        "/sys/module/pinctrl_bcm2835/parameters/persist_gpio_outputs")


# -- a board that energizes its relay on a low input ------------------------

@pytest.mark.parametrize("polarity,connected,expected", [
    ("energize_to_shed", True, True),
    ("energize_to_shed", False, False),
    ("energize_to_connect", True, False),
    ("energize_to_connect", False, True),
])
def test_on_a_low_trigger_board_the_pin_carries_the_inverse_voltage(
        pins, polarity, connected, expected):
    """Read from the pin through the real library. An energized coil on a
    low trigger board is a pin driven low."""
    pin = relay.Relay(23, polarity, relay.LOW)
    pin.apply(connected)
    assert pin._device.pin.state is expected
    pin.close()


def test_claiming_a_pin_on_a_low_trigger_board_leaves_it_high(pins):
    """High is a de-energized coil on that board. A pin claimed low would
    cut the cameras and start the pump for the width of the claim, every
    time the controller starts."""
    for polarity in relay.POLARITIES:
        pin = relay.Relay(24, polarity, relay.LOW)
        pin._open()
        assert pin._device.pin.state is True, polarity
        pin.close()


def test_apply_reports_whether_the_coil_is_energized_whatever_the_trigger():
    """The loop publishes this, and a dashboard reads it as the relay's
    state, so it must not change meaning with the board."""
    for trigger in relay.TRIGGERS:
        pin = relay.Relay(23, relay.ENERGIZE_TO_SHED, trigger,
                          factory=relay.Recording)
        assert pin.apply(True) is False, trigger
        assert pin.apply(False) is True, trigger


def test_the_trigger_reaches_the_device_as_its_active_level():
    high = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.HIGH,
                       factory=relay.Recording)
    low = relay.Relay(23, relay.ENERGIZE_TO_SHED, relay.LOW,
                      factory=relay.Recording)
    high._open()
    low._open()
    assert high._device.active_high is True
    assert low._device.active_high is False


@pytest.mark.parametrize("trigger", [None, "", "active_low", True, "LOW"])
def test_an_unknown_trigger_is_refused(trigger):
    """Guessing it drives every load backwards, and nothing reveals that
    until a load is seen doing the opposite of its decision."""
    with pytest.raises(relay.RelayError) as raised:
        relay.Relay(23, relay.ENERGIZE_TO_SHED, trigger)
    assert "GPIO23" in str(raised.value)


def test_a_pretend_relay_keeps_the_trigger():
    pretend = relay.Pretend(23, relay.ENERGIZE_TO_SHED, relay.LOW)
    pretend.apply(True)
    assert pretend._device.active_high is False


# -- claiming an output at its starting level -------------------------------

class FakeLgpio:
    """Stands in for lgpio, which exists only on the machine, and records
    every call made through it."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.isupper():
            return 0
        def call(*args):
            self.calls.append((name,) + args)
            return 0
        return call


@pytest.fixture
def lgpio(monkeypatch):
    import sys
    import types
    fake = FakeLgpio()
    module = types.ModuleType("lgpio")
    module.__getattr__ = fake.__getattr__
    monkeypatch.setitem(sys.modules, "lgpio", module)
    monkeypatch.delitem(sys.modules, "gpiozero.pins.lgpio", raising=False)
    monkeypatch.setattr(relay, "_LGPIO_CLASSES", None)
    return fake


@pytest.mark.parametrize("state,level", [(True, 1), (False, 0)])
def test_an_output_is_claimed_at_its_starting_level_in_one_call(lgpio, state,
                                                                level):
    """A low trigger board energizes its coil for as long as a pin claimed
    at 0 waits to be written high. Claiming at the level leaves no wait."""
    import types
    _, pin_class = relay.lgpio_classes()
    pin = object.__new__(pin_class)
    pin._factory = types.SimpleNamespace(_handle=7)
    pin._number = 23
    pin._callback = None
    pin.output_with_state(state)
    assert lgpio.calls == [("gpio_claim_output", 7, 23, level)]


def test_every_claim_goes_through_the_controller_factory(monkeypatch):
    """Passed to the device, not left to the library's default, which
    anything else in the process could have set first."""
    import sys
    import types
    made = []
    module = types.ModuleType("gpiozero")
    module.DigitalOutputDevice = lambda pin, **kwargs: made.append(kwargs)
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    factory = object()
    monkeypatch.setattr(relay, "controller_factory", lambda: factory)
    relay.open_output(23)
    assert made[0]["pin_factory"] is factory


def test_a_pin_built_through_another_backend_is_refused():
    class Ours:
        pass

    class Stock:
        pass

    with pytest.raises(relay.RelayError) as raised:
        relay.own_pin(Stock(), Ours, 23)
    assert "GPIO23" in str(raised.value)
    found = Ours()
    assert relay.own_pin(found, Ours, 23) is found


def test_the_factory_classes_are_built_once(lgpio):
    """A second pair would not recognize the pins the first built, and would
    refuse every one of them."""
    assert relay.lgpio_classes() is relay.lgpio_classes()


# -- the controller's factory, through gpiozero's own pin cache ---------------

class RecordingLgpio:
    """lgpio, recording every call, answering the mode of a line from the
    last claim made on it."""

    SET_PULL_NONE = 128

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.isupper():
            return 0
        if name == "error":
            return RuntimeError

        def call(*args):
            self.calls.append((name,) + args)
            if name == "gpiochip_open":
                return 9
            if name == "gpio_get_mode":
                for made in reversed(self.calls[:-1]):
                    if (made[0].startswith("gpio_claim")
                            and made[2] == args[1]):
                        return 2 if made[0] == "gpio_claim_output" else 0
            return 0
        return call

    def claims(self):
        return [c for c in self.calls if c[0].startswith("gpio_claim")]


@pytest.fixture
def board(monkeypatch):
    """The real gpiozero lgpio backend over a recording lgpio, on a fixed
    board revision, with the library's shared pin cache emptied first and
    put back after."""
    import sys
    import types
    from collections import defaultdict
    fake = RecordingLgpio()
    module = types.ModuleType("lgpio")
    module.__getattr__ = fake.__getattr__
    module.SET_PULL_NONE = RecordingLgpio.SET_PULL_NONE
    monkeypatch.setitem(sys.modules, "lgpio", module)
    monkeypatch.delitem(sys.modules, "gpiozero.pins.lgpio", raising=False)
    from gpiozero.pins import local
    monkeypatch.setattr(local, "get_pi_revision", lambda: 0xa02082)
    monkeypatch.setattr(local.LocalPiFactory, "pins", {})
    monkeypatch.setattr(local.LocalPiFactory, "_reservations",
                        defaultdict(list))
    monkeypatch.setattr(relay, "_LGPIO_CLASSES", None)
    monkeypatch.setattr(relay, "_FACTORY", None)
    return fake


def test_a_low_trigger_relay_is_claimed_high_in_one_call_through_the_library(
        board):
    """The whole path the machine takes: the library's own device and pin
    cache, the controller's factory, and an lgpio that only records."""
    device = relay.open_output(23, active_high=False)
    try:
        assert board.claims()[-1] == ("gpio_claim_output", 9, 23, 1)
        assert not [c for c in board.claims()
                    if c[0] == "gpio_claim_output" and c[3] == 0]
    finally:
        device.close()


def test_a_pin_the_stock_backend_built_is_refused_by_the_controller(board):
    """The library shares one pin cache between every factory of its kind,
    so a pin built through its own backend would come back here and claim
    at 0."""
    from gpiozero.pins.lgpio import LGPIOFactory
    LGPIOFactory().pin(24)
    with pytest.raises(relay.RelayError) as raised:
        relay.open_output(24, active_high=False)
    assert "GPIO24" in str(raised.value)
