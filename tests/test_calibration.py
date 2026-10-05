from monitoring import calibration


def test_scale_and_offset_are_applied():
    out = calibration.apply({"voltage": 12.0}, {"voltage": {"scale": 1.1, "offset": 0.3}})
    assert round(out["voltage"], 4) == 13.5


def test_missing_parts_default_to_identity():
    assert calibration.apply({"v": 5.0}, {"v": {"offset": 1.0}})["v"] == 6.0
    assert calibration.apply({"v": 5.0}, {"v": {"scale": 2.0}})["v"] == 10.0


def test_fields_not_named_are_untouched():
    out = calibration.apply({"a": 1.0, "b": 2.0}, {"a": {"offset": 1.0}})
    assert out == {"a": 2.0, "b": 2.0}


def test_a_string_field_is_left_alone_rather_than_raising():
    """A field that changed type upstream must not cost the whole reading."""
    out = calibration.apply({"state": "float"}, {"state": {"scale": 2.0}})
    assert out == {"state": "float"}


def test_the_input_is_not_mutated():
    original = {"v": 1.0}
    calibration.apply(original, {"v": {"offset": 5.0}})
    assert original == {"v": 1.0}
