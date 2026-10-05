"""Parse the duration strings used throughout a node file.

Durations are written the way a person writes them, as "30s" or "6h", because
a node file is edited by hand far more often than it is read by code.
"""

UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class DurationError(ValueError):
    """A duration string that cannot be understood."""


def seconds(value, what="duration"):
    """Return `value` as seconds.

    Accepts a number, which is taken as seconds already, or a string with one
    of the unit suffixes. Anything else raises, because a silently wrong
    interval is worse than a refusal to start.
    """
    if isinstance(value, bool):
        raise DurationError("%s must be a duration, not a boolean" % what)
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise DurationError("%s must be a duration string such as 30s" % what)

    text = value.strip().lower()
    for suffix in sorted(UNITS, key=len, reverse=True):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            try:
                return float(number) * UNITS[suffix]
            except ValueError:
                break
    raise DurationError(
        "%s: cannot read %r as a duration; use a number with one of %s"
        % (what, value, ", ".join(sorted(UNITS)))
    )
