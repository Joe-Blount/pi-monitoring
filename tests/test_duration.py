import pytest

from monitoring import duration


@pytest.mark.parametrize("text,expected", [
    ("30s", 30.0), ("5m", 300.0), ("2h", 7200.0), ("1d", 86400.0),
    ("500ms", 0.5), ("1.5h", 5400.0), (" 45S ", 45.0), (90, 90.0),
])
def test_understood_durations(text, expected):
    assert duration.seconds(text) == expected


@pytest.mark.parametrize("text", ["", "soon", "30x", "m", True, None, ["30s"]])
def test_refused_durations(text):
    """A silently wrong interval is worse than a refusal to start."""
    with pytest.raises(duration.DurationError):
        duration.seconds(text)


def test_the_message_names_what_failed():
    with pytest.raises(duration.DurationError) as exc:
        duration.seconds("soon", "device mppt interval")
    assert "device mppt interval" in str(exc.value)
