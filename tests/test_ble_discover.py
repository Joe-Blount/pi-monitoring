"""The parts of the discovery tool that work with no radio.

Everything that decides something is here: which characteristics are worth
subscribing to, which protocol family the identifiers suggest, and what the
probe frames are. Only the connecting is left untested, because only the
connecting needs a pack.
"""

import pathlib
import runpy
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = str(REPO / "bin" / "ble-discover")

#: A pack that speaks ABC-BMS, as bleak would report it.
SOK_TREE = [
    ("0000ffe0-0000-1000-8000-00805f9b34fb", [
        ("0000ffe1-0000-1000-8000-00805f9b34fb", ["notify"]),
        ("0000ffe2-0000-1000-8000-00805f9b34fb", ["write-without-response"]),
    ]),
    ("00001800-0000-1000-8000-00805f9b34fb", [
        ("00002a00-0000-1000-8000-00805f9b34fb", ["read"]),
    ]),
]

#: A pack with nothing recognisable, which is the case this tool exists for.
UNKNOWN_TREE = [
    ("0000abcd-0000-1000-8000-00805f9b34fb", [
        ("0000abce-0000-1000-8000-00805f9b34fb", ["notify", "read"]),
        ("0000abcf-0000-1000-8000-00805f9b34fb", ["write"]),
    ]),
]


@pytest.fixture(scope="module")
def tool():
    """Load the script as a module without running its main()."""
    saved = sys.argv
    sys.argv = ["ble-discover"]
    try:
        return runpy.run_path(SCRIPT, run_name="not_main")
    finally:
        sys.argv = saved


# --- identifying what was found -------------------------------------------

def test_a_known_pack_is_recognised(tool):
    """Recognising the family is most of the value. It turns writing a driver
    into checking an existing one against real frames."""
    assert tool["guess_family"](SOK_TREE) == ["abc_bms"]


def test_an_unknown_pack_is_reported_as_unknown(tool):
    """Guessing wrong is worse than not guessing. A wrong family sends
    commands a pack may not understand and wastes the one visit."""
    assert tool["guess_family"](UNKNOWN_TREE) == []


def test_a_family_needs_both_its_characteristics_to_match(tool):
    """One identifier in common is a coincidence. A pack with the notify
    characteristic but no way to write to it is not that family."""
    half = [("0000ffe0-0000-1000-8000-00805f9b34fb", [
        ("0000ffe1-0000-1000-8000-00805f9b34fb", ["notify"]),
    ])]
    assert tool["guess_family"](half) == []


def test_notify_and_indicate_are_both_worth_subscribing_to(tool):
    """Some packs indicate rather than notify. Looking only for notify finds
    nothing on those, and the tool reports a pack with nothing to say."""
    tree = [("0000abcd-0000-1000-8000-00805f9b34fb", [
        ("0000abce-0000-1000-8000-00805f9b34fb", ["indicate"]),
        ("0000abcf-0000-1000-8000-00805f9b34fb", ["read"]),
    ])]
    assert tool["notifying"](tree) == ["0000abce-0000-1000-8000-00805f9b34fb"]


def test_write_without_response_counts_as_writable(tool):
    """These packs are written to without a response, so a tool that lists
    only plain write reports no way to send a command."""
    found = [tool["short_uuid"](c) for c in tool["writable"](SOK_TREE)]
    assert found == ["ffe2"]


def test_a_read_only_characteristic_is_not_offered_for_writing(tool):
    assert tool["writable"](SOK_TREE[1:]) == []


# --- rendering -------------------------------------------------------------

def test_the_standard_identifier_pattern_is_shortened(tool):
    """Everybody quotes the sixteen bit part. Printing all of it makes the
    output unreadable on a phone over ssh, which is where it gets read."""
    assert tool["short_uuid"]("0000ffe1-0000-1000-8000-00805f9b34fb") == "ffe1"


def test_a_nonstandard_identifier_is_left_alone(tool):
    """A vendor identifier has no sixteen bit short form, and truncating it
    would merge two different characteristics into one row."""
    full = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    assert tool["short_uuid"](full) == full


def test_the_tree_names_the_characteristics_it_recognises(tool):
    lines = "\n".join(tool["describe_tree"](SOK_TREE))
    assert "ffe0" in lines and "ABC-BMS" in lines
    assert "ffe1" in lines and "ffe2" in lines


def test_the_tree_still_renders_a_pack_it_does_not_recognise(tool):
    """The unknown case is the one this tool exists for, so it must not
    depend on recognising anything."""
    lines = "\n".join(tool["describe_tree"](UNKNOWN_TREE))
    assert "abce" in lines and "notify" in lines


def test_a_frame_is_printed_with_its_time_and_source(tool):
    """Matching a reply to the command that caused it needs both."""
    line = tool["format_frame"](1.5, "0000ffe1-0000-1000-8000-00805f9b34fb",
                                b"\xcc\xf0\x01")
    assert "1.500" in line and "ffe1" in line and "cc f0 01" in line


# --- the probe frames ------------------------------------------------------

def test_the_abc_bms_probes_match_what_the_driver_sends(tool):
    """If these differ from the driver, a pack can answer the tool and then
    fail against the driver, which is the most confusing outcome available."""
    from monitoring.drivers import abc_bms

    built = tool["commands_for"]("abc_bms")
    expected = [abc_bms.command(code) for code in (0xC0, 0xC1, 0xC2, 0xC4)]
    assert built == expected


def test_the_jbd_probe_carries_a_correct_checksum(tool):
    """Sum of the payload, two's complement, high byte first. A wrong
    checksum is rejected silently and looks exactly like a pack that does not
    speak the protocol."""
    frame = tool["commands_for"]("jbd")[0]
    assert frame[0] == 0xDD and frame[1] == 0xA5 and frame[-1] == 0x77
    payload = frame[2:4]
    checksum = (frame[4] << 8) | frame[5]
    assert (sum(payload) + checksum) & 0xFFFF == 0


def test_every_family_can_build_its_probes(tool):
    """A family declared with no builder, or a builder that does not exist,
    fails at the pack rather than here."""
    for name in tool["FAMILIES"]:
        frames = tool["commands_for"](name)
        assert frames and all(isinstance(f, bytes) and f for f in frames)


def test_no_probe_can_change_anything_on_a_battery(tool):
    """These protocols accept unauthenticated commands that switch the charge
    and discharge paths. A diagnostic must not be able to send one, so the
    command bytes are checked against the read-only sets."""
    readonly = {"abc_bms": {0xC0, 0xC1, 0xC2, 0xC4}, "jbd": {0x03, 0x04, 0x05}}
    for name, spec in tool["FAMILIES"].items():
        codes = {int(code, 16) for code in spec["commands"]}
        assert codes <= readonly[name], "%s sends %s" % (name, codes - readonly[name])


def test_the_dallas_checksum_matches_the_driver(tool):
    from monitoring.drivers import abc_bms

    for sample in (b"", b"\x00", b"\xee\xc1\x00\x00\x00", bytes(range(32))):
        assert tool["crc8_dallas"](sample) == abc_bms.crc8(sample)
