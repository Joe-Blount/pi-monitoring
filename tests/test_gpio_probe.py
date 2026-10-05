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
    boot_config(probe, monkeypatch, "dtoverlay=w1-gpio\n", tmp_path)
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
