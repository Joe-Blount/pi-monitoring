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

    One entry is a one-byte index, a two-byte little-endian millivolt reading,
    and one byte of padding, which is what a real pack sends. Four entries
    fill a frame.
    """
    out = []
    for start in range(0, len(millivolts), per_frame):
        payload = bytearray()
        for offset, value in enumerate(millivolts[start:start + per_frame]):
            payload.append(start + offset + 1)
            payload += struct.pack("<H", value)
            payload.append(0)
        out.append(frame(abc_bms.CELL_MESSAGE, payload))
    return out


#: Frames captured from a SOK-48V0045 under load, with the vendor application
#: showing 100 Ah rated, 106.05 Ah actual, 212 cycles, two sensors at 25 C and
#: 24 C, the heater off, and sixteen cells near 3300 mV.
#:
#: Generated fixtures prove the parser is self-consistent. Only a capture
#: proves it agrees with a battery.
REAL = [
    "cc f1 53 4f 4b 2d 42 4d 53 0d 00 00 00 00 00 00 00 00 00 40",
    "cc f0 2c ce 00 d8 ad ff a0 86 01 40 9e 01 d4 00 63 00 00 74",
    "cc f2 01 01 02 19 00 18 00 00 00 00 00 01 00 00 00 00 00 e7",
    "cc f3 17 03 01 00 c8 00 00 01 00 00 00 00 00 00 00 00 00 b5",
    "cc f4 01 e8 0c 00 02 dc 0c 00 03 e6 0c 00 04 df 0c 00 00 40",
    "cc f4 05 e0 0c 00 06 e6 0c 00 07 e0 0c 00 08 e6 0c 00 00 45",
    "cc f4 09 e2 0c 00 0a e0 0c 00 0b e0 0c 00 0c e1 0c 00 00 7d",
    "cc f4 0d e6 0c 00 0e e2 0c 00 0f e6 0c 00 10 dc 0c 00 00 04",
    "cc f9 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ed",
]

#: What the vendor application displayed while those frames were captured.
REAL_CELL_MILLIVOLTS = [3304, 3292, 3302, 3295, 3296, 3302, 3296, 3302,
                        3298, 3296, 3296, 3297, 3302, 3298, 3302, 3292]


def real_messages():
    """The captured frames, gathered as the transport would gather them."""
    messages = {}
    for hexed in REAL:
        abc_bms.collect(messages, bytes.fromhex(hexed.replace(" ", "")))
    return messages


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
