"""The probe is a diagnostic, but its refusal to touch a wired pin is a safety
feature and is tested like one."""

import pathlib
import runpy
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE = str(REPO / "bin" / "gpio-probe")


@pytest.fixture(scope="module")
def probe():
    """Load the script as a module without running its main()."""
    saved = sys.argv
    sys.argv = ["gpio-probe"]
    try:
        namespace = runpy.run_path(PROBE, run_name="not_main")
    finally:
        sys.argv = saved
    return namespace


@pytest.mark.parametrize("pin,reason", [
    (6, "rain gauge"),
    (17, "DHT"),
])
def test_a_pin_declared_in_the_node_file_is_refused(probe, pin, reason, capsys):
    """Driving a pin that is wired to a sensor could damage it, and would at
    best produce a meaningless result."""
    allowed = probe["check_pin_is_free"](pin, str(REPO / "sites/blind1/upstairs.yaml"))
    assert allowed is False
    assert "Refusing" in capsys.readouterr().out


@pytest.mark.parametrize("pin", sorted({0, 1, 14, 15}))
def test_pins_reserved_by_the_board_are_refused(probe, pin, capsys):
    assert probe["check_pin_is_safe"](pin) is False
    assert "Refusing" in capsys.readouterr().out


def boot_config(probe, monkeypatch, text, tmp_path):
    """Point the probe at a fake boot configuration.

    Patching the namespace runpy hands back does not work: the functions close
    over their own globals, so the copy is not what they read.
    """
    config = tmp_path / "config.txt"
    config.write_text(text)
    monkeypatch.setitem(probe["overlay_pins"].__globals__,
                        "BOOT_CONFIGS", (str(config),))


def test_a_pin_taken_by_an_overlay_is_refused_though_no_node_file_mentions_it(
        probe, tmp_path, monkeypatch, capsys):
    """The gap this check exists for. A one-wire sensor is addressed by its own
    id, so nothing in a node file says GPIO4 is carrying a bus. Driving it
    would disturb every sensor on that wire."""
    boot_config(probe, monkeypatch, "dtparam=i2c_arm=on\ndtoverlay=w1-gpio\n", tmp_path)
    taken = probe["overlay_pins"]()
    assert 4 in taken and "one-wire" in taken[4]
    assert 2 in taken and 3 in taken

    assert probe["check_pin_is_safe"](4) is False
    assert "Refusing" in capsys.readouterr().out


def test_a_relocated_one_wire_pin_is_found(probe, tmp_path, monkeypatch):
    boot_config(probe, monkeypatch, "dtoverlay=w1-gpio,gpiopin=22\n", tmp_path)
    taken = probe["overlay_pins"]()
    assert 22 in taken and 4 not in taken


def test_a_commented_out_overlay_is_ignored(probe, tmp_path, monkeypatch):
    boot_config(probe, monkeypatch, "#dtoverlay=w1-gpio\n", tmp_path)
    assert probe["overlay_pins"]() == {}


def test_a_pin_no_overlay_claims_is_allowed(probe, tmp_path, monkeypatch):
    """The live-pin checks are stubbed out. Otherwise this runs raspi-gpio and
    reads the kernel debug file on whatever machine happens to run the suite,
    so on a real Pi the verdict would depend on that pin's current state."""
    boot_config(probe, monkeypatch, "dtoverlay=w1-gpio\n", tmp_path)
    monkeypatch.setitem(probe["check_pin_is_safe"].__globals__,
                        "debugfs_consumers", lambda: [])
    monkeypatch.setitem(probe["check_pin_is_safe"].__globals__,
                        "read_pin", lambda _pin: None)
    assert probe["check_pin_is_safe"](26) is True


def test_an_unused_pin_is_allowed(probe, capsys):
    assert probe["check_pin_is_free"](26, str(REPO / "sites/blind1/upstairs.yaml"))
    assert "not declared" in capsys.readouterr().out


def test_a_control_pin_is_refused_too(probe, capsys):
    """Control pins are claimed in a different part of the file and must be
    refused by the same check."""
    assert probe["check_pin_is_free"](
        23, str(REPO / "sites/blind1/downstairs.yaml")) is False


def test_no_node_file_means_no_opinion(probe):
    assert probe["check_pin_is_free"](26, None) is True


def test_an_unreadable_node_file_does_not_block_the_probe(probe, capsys):
    """A diagnostic that refuses to run because a config file is missing is
    useless on the machine where you most need it."""
    assert probe["check_pin_is_free"](26, "/nonexistent.yaml") is True


def test_a_pin_still_driving_after_a_kill_is_reported_as_a_failure(probe, capsys):
    """The central question. A pass here is what the whole energize-to-connect
    design rests on, so a wrong answer must be loud."""
    code = probe["verdict"]({"after_kill": {"level": "1", "func": "OUTPUT"}})
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out and "does NOT hold" in out
    assert "lgpio" in out, "the message must say how to fix it"


def test_a_released_pin_is_reported_as_a_pass(probe, capsys):
    code = probe["verdict"]({"after_kill": {"level": "0", "func": "INPUT"}})
    assert code == 0
    assert "PASS" in capsys.readouterr().out


def test_an_unreadable_pin_is_neither_pass_nor_fail(probe, capsys):
    """Not knowing must not look like a pass."""
    code = probe["verdict"]({"after_kill": None})
    out = capsys.readouterr().out
    assert code == 2
    assert "UNANSWERED" in out


def test_sharing_an_input_line_is_reported_either_way(probe, capsys):
    probe["verdict"]({"after_kill": {"level": "0", "func": "INPUT"},
                      "shared_input": False})
    assert "needs its own pin" in capsys.readouterr().out


@pytest.mark.parametrize("pin,what", [
    (23, "camera branch control"),
    (25, "inverter relay"),
    (22, "pump contactor"),
])
def test_pins_nested_under_a_load_are_refused(probe, pin, what, capsys):
    """These live two levels down in the node file. A second copy of the pin
    map missed them, so the probe would have driven the inverter relay while
    reporting the pin as free."""
    allowed = probe["check_pin_is_free"](
        pin, str(REPO / "sites/blind1/downstairs.yaml"))
    assert allowed is False, "%s (GPIO%d) must be refused" % (what, pin)
    assert "Refusing" in capsys.readouterr().out


def test_a_dht_overlay_pin_is_refused(probe, tmp_path, monkeypatch, capsys):
    """Nothing in a node file binds the DHT pin: the overlay does. Without
    this the probe drives a live sensor's data line."""
    boot_config(probe, monkeypatch, "dtoverlay=dht11,gpiopin=17\n", tmp_path)
    taken = probe["overlay_pins"]()
    assert 17 in taken and "DHT" in taken[17]


def test_a_dht_overlay_without_a_pin_defaults_to_four(probe, tmp_path, monkeypatch):
    boot_config(probe, monkeypatch, "dtoverlay=dht11\n", tmp_path)
    assert 4 in probe["overlay_pins"]()


def test_a_pin_left_as_a_driving_output_is_not_called_a_pass(probe, capsys):
    """Still an output driving low is not released. Safe with an active-high
    module, energized with an active-low one, and false either way."""
    code = probe["verdict"]({"after_kill": {"level": "0", "func": "OUTPUT"}})
    out = capsys.readouterr().out
    assert code == 1
    assert "INCONCLUSIVE" in out
    assert "PASS" not in out


def test_the_remedy_names_its_own_prerequisite(probe, capsys):
    """The older of the two deployed systems ships gpiozero 1.6.2, which has
    no lgpio factory, so the advice cannot be followed there as written."""
    probe["verdict"]({"after_kill": {"level": "1", "func": "OUTPUT"}})
    out = capsys.readouterr().out
    assert "Bullseye" in out or "gpiozero 2" in out


def test_a_failed_probe_still_kills_the_process_holding_the_pin(probe, monkeypatch):
    """Without this an exception between starting the holder and killing it
    leaves the pin driven high until the machine reboots, with whatever is
    attached to it energized. It is the only hardware-safety path here."""
    killed = []

    class FakeHolder:
        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def kill(self):
            killed.append(True)
            self.alive = False

        def wait(self, timeout=None):
            return 0

    monkeypatch.setitem(probe["run_checks"].__globals__,
                        "spawn_holder", lambda pin: FakeHolder())
    monkeypatch.setitem(probe["run_checks"].__globals__,
                        "read_pin", lambda pin: {"level": "1", "func": "OUTPUT"})

    # Fail only after a holder is running. Raising on the baseline reading,
    # before anything has been started, would prove nothing.
    calls = []

    def explode_after_the_holder_starts(*_args, **_kwargs):
        calls.append(True)
        if len(calls) > 1:
            raise RuntimeError("the probe failed partway through")
        return "level=0  func=INPUT"

    monkeypatch.setitem(probe["run_checks"].__globals__, "describe",
                        explode_after_the_holder_starts)

    with pytest.raises(RuntimeError):
        probe["run_checks"](26)
    assert killed, "the holder was left running with the pin driven high"
