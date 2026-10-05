"""Build ABC-BMS frames with correct checksums.

Generated rather than captured, so a test can ask for the awkward case: a
sixteen-cell pack spread across several frames, a frame with a bad checksum,
a value one byte out of place.
"""

import struct

from monitoring.drivers import abc_bms


def frame(message_id, payload):
    """One twenty-byte reply carrying `payload`, padded and checksummed."""
    body = bytearray([abc_bms.RESPONSE_HEAD, message_id])
    body += bytes(payload)[:abc_bms.FRAME_LENGTH - 3]
    body += b"\x00" * (abc_bms.FRAME_LENGTH - 1 - len(body))
    body.append(abc_bms.crc8(bytes(body)))
    assert len(body) == abc_bms.FRAME_LENGTH
    return bytes(body)


def status_frame(volts=53.2, amps=-12.5, design_ah=100.0, remaining_ah=78.0,
                 cycles=42, soc=78):
    """Message F0: the one carrying everything scalar."""
    payload = bytearray(18)

    def put(offset, value, length, signed=False):
        payload[offset - 2:offset - 2 + length] = int(round(value)).to_bytes(
            length, "little", signed=signed)

    put(2, volts * 1000, 3)
    put(5, amps * 1000, 3, signed=True)
    put(8, design_ah * 1000, 3)
    put(11, remaining_ah * 1000, 3)
    put(14, cycles, 2)
    put(16, soc, 1)
    return frame(0xF0, payload)


def cell_frames(millivolts, per_frame=4):
    """Cell voltages split across frames the way a pack sends them.

    Each entry is a two-byte cell index and a two-byte voltage. That layout is
    an assumption, not a confirmed fact, which is why the parser checks the
    result rather than trusting the stride, and why these tests prove the
    parser refuses a layout it cannot make sense of.
    """
    out = []
    for start in range(0, len(millivolts), per_frame):
        payload = bytearray()
        for offset, value in enumerate(millivolts[start:start + per_frame]):
            payload += struct.pack("<HH", start + offset + 1, value)
        out.append(frame(abc_bms.CELL_MESSAGE, payload))
    return out


def corrupt(data):
    """A frame that will fail its checksum."""
    broken = bytearray(data)
    broken[-1] ^= 0xFF
    return bytes(broken)


#: Sixteen cells of a 48 V pack, slightly out of balance.
SIXTEEN_CELLS = [3320, 3322, 3319, 3325, 3321, 3320, 3318, 3324,
                 3323, 3319, 3321, 3326, 3320, 3317, 3322, 3321]

#: Four cells of a 12 V pack, which the same reader must also handle.
FOUR_CELLS = [3330, 3328, 3331, 3329]
