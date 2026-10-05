"""Build InfluxDB line protocol.

This is the seam the whole project is shaped around. A driver reads a device
and produces a line; nothing downstream of here knows what a sensor is, and
nothing upstream knows what InfluxDB is.

Two rules are enforced here rather than left to each driver, because both have
already caused outages when left to good intentions:

Numeric fields are always written as floats. InfluxDB rejects a field whose
type changes between writes, and a driver that happens to return a whole
number once will otherwise poison that field forever.

Yes-or-no values are written as 0.0 and 1.0 rather than as booleans, so that a
daily mean reads directly as the fraction of the day a condition held. A
boolean cannot be averaged.
"""


class LineProtocolError(ValueError):
    """A point that cannot be written as line protocol."""


def _without_line_breaks(value):
    """Line protocol has no escape for a line break: a newline ends the point.

    So they are replaced wherever they can appear, rather than escaped. Tag
    values are normally validated long before they reach here, which makes
    this defence in depth rather than a routine path, but a point split in two
    leaves an unterminated quote and telegraf fails the whole batch.
    """
    text = str(value)
    for line_break in ("\r\n", "\n", "\r"):
        text = text.replace(line_break, " ")
    return text


def _escape(value, specials):
    out = _without_line_breaks(value)
    for char in specials:
        out = out.replace(char, "\\" + char)
    return out


def escape_measurement(name):
    """Measurement names escape comma and space."""
    return _escape(name, [",", " "])


def escape_key(name):
    """Tag keys, tag values and field keys escape comma, equals and space."""
    return _escape(name, [",", "=", " "])


def escape_string_field(value):
    """String field values are quoted, escaping backslash and double quote.

    Line breaks are removed rather than escaped: line protocol has no escape
    for them, so one exception message containing a newline would split a
    point in two and telegraf would fail the whole batch.
    """
    return '"%s"' % _without_line_breaks(value).replace(
        "\\", "\\\\").replace('"', '\\"')


def format_field(name, value):
    """Render one field, choosing the representation from the value's type."""
    if isinstance(value, bool):
        # Deliberately not written as a boolean. See the module docstring.
        return "%s=%s" % (escape_key(name), "1.0" if value else "0.0")
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise LineProtocolError(
                "field %r is %r, which InfluxDB cannot store" % (name, number)
            )
        return "%s=%s" % (escape_key(name), repr(number))
    if isinstance(value, str):
        return "%s=%s" % (escape_key(name), escape_string_field(value))
    raise LineProtocolError(
        "field %r has type %s; only numbers and strings can be written"
        % (name, type(value).__name__)
    )


def line(measurement, tags, fields, timestamp_ns=None):
    """Return one line of InfluxDB line protocol.

    Fields whose value is None are dropped rather than written, because a
    sensor that could not read one value should not lose the others.

    Tags are emitted in sorted order. InfluxDB treats tag order as
    insignificant but stores points more efficiently when it is consistent.
    """
    live = {name: value for name, value in (fields or {}).items() if value is not None}
    if not live:
        raise LineProtocolError(
            "measurement %r has no fields; a point must carry at least one"
            % measurement
        )

    parts = [escape_measurement(measurement)]
    for name in sorted((tags or {})):
        value = tags[name]
        if value is None or value == "":
            continue
        parts.append("%s=%s" % (escape_key(name), escape_key(value)))

    rendered = ",".join(format_field(name, live[name]) for name in sorted(live))
    out = "%s %s" % (",".join(parts), rendered)
    if timestamp_ns is not None:
        out = "%s %d" % (out, int(timestamp_ns))
    return out
