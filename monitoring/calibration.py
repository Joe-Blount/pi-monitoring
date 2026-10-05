"""Apply per-field scale and offset.

A correction belongs to one installation, not to the code. Two sensors of the
same model in two places need different numbers, and a reading that is wrong
by a known amount should be fixed by editing a node file rather than by
patching a driver.

Applied in one place so there is one answer to "where did this number get
changed".
"""


def apply(fields, calibration):
    """Return `fields` with scale and offset applied.

    Only numeric fields are touched. A string field named in the calibration
    is left alone rather than raising, because the common cause is a field
    that changed type in a firmware update, and dropping the whole reading
    over it would lose the other fields too.
    """
    if not calibration:
        return fields

    out = dict(fields)
    for name, correction in calibration.items():
        if name not in out:
            continue
        value = out[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        scale = float(correction.get("scale", 1.0))
        offset = float(correction.get("offset", 0.0))
        out[name] = value * scale + offset
    return out
