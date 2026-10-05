import math

import pytest

from monitoring import lineproto


def test_numbers_are_always_floats():
    """A whole number must not be written as an integer.

    InfluxDB rejects a field whose type changes between writes, so a driver
    that happens to return 5 once would otherwise poison that field forever.
    """
    out = lineproto.line("m", {}, {"count": 5})
    assert out == "m count=5.0"


def test_yes_or_no_is_a_float_not_a_boolean():
    """A boolean cannot be averaged; 0.0 and 1.0 give a duty cycle directly."""
    assert lineproto.line("m", {}, {"on": True}) == "m on=1.0"
    assert lineproto.line("m", {}, {"on": False}) == "m on=0.0"


def test_strings_are_quoted_and_escaped():
    out = lineproto.line("m", {}, {"note": 'say "hi"\\there'})
    assert out == 'm note="say \\"hi\\"\\\\there"'


def test_tags_are_sorted_and_escaped():
    out = lineproto.line("m", {"z": "1", "a": "two words", "b": "a,b"}, {"v": 1.0})
    assert out == "m,a=two\\ words,b=a\\,b,z=1 v=1.0"


def test_measurement_is_escaped():
    assert lineproto.line("odd name", {}, {"v": 1.0}).startswith("odd\\ name ")


def test_none_fields_are_dropped_not_written():
    """One unreadable value must not cost the readings beside it."""
    out = lineproto.line("m", {}, {"a": 1.0, "b": None})
    assert out == "m a=1.0"


def test_empty_tag_values_are_dropped():
    out = lineproto.line("m", {"a": "", "b": None, "c": "x"}, {"v": 1.0})
    assert out == "m,c=x v=1.0"


def test_a_point_with_no_fields_is_refused():
    with pytest.raises(lineproto.LineProtocolError):
        lineproto.line("m", {"a": "b"}, {"only": None})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -math.inf])
def test_unstorable_numbers_are_refused(value):
    with pytest.raises(lineproto.LineProtocolError):
        lineproto.line("m", {}, {"v": value})


def test_unsupported_type_is_refused():
    with pytest.raises(lineproto.LineProtocolError):
        lineproto.line("m", {}, {"v": [1, 2]})


def test_timestamp_is_appended_when_given():
    assert lineproto.line("m", {}, {"v": 1.0}, 1700000000000000000) == \
        "m v=1.0 1700000000000000000"
