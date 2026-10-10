"""JBD frames, both captured and generated.

The captured ones prove the driver agrees with a battery. The generated ones
let a test ask for a case the battery did not happen to be in: a fault, a
different cell count, a damaged checksum.
"""

import struct

from monitoring.drivers import jbd

#: Captured from a Chins 320 Ah pack, hardware J-12320-250304-077, while it was
#: charging at 7.21 A with a 53 percent state of charge and one sensor at
#: 23.2 C. The basic information reply arrived as three notifications, which is
#: the whole reason the reader reassembles by length.
REAL_NOTIFICATIONS = [
    "dd 03 00 22 05 39 02 d1 42 0c 7d 00 00 55 32 61 00 00 00 00",
    "00 00 2a 35 03 04 01 0b 93 00 00 00 7d 00 42 0c 00 00 fb 4a",
    "77",
    "dd 04 00 08 0d 1a 0d 10 0d 17 0d 0e ff 75 77",
    "dd 05 00 12 4a 2d 31 32 33 32 30 2d 32 35 30 33 30 34 2d 30",
    "37 37 fc 59 77",
]

#: What the pack reported at that moment, for the tests to assert against.
REAL_CELL_MILLIVOLTS = [3354, 3344, 3351, 3342]
REAL_HARDWARE = "J-12320-250304-077"


def real_messages():
    """The captured replies, gathered as the transport gathers them."""
    messages = {}
    buffer = bytearray()
    for chunk in REAL_NOTIFICATIONS:
        buffer.extend(bytes.fromhex(chunk.replace(" ", "")))
        complete, rest = jbd.frames(buffer)
        buffer[:] = rest
        for frame in complete:
            assert jbd.frame_is_valid(frame) is None, jbd.frame_is_valid(frame)
            messages[frame[1]] = jbd.payload(frame)
    return messages


def frame(register, body, status=0x00):
    """Wrap a payload as a reply, with a correct checksum."""
    inner = bytes([len(body)]) + bytes(body)
    return (bytes([jbd.START, register, status]) + inner
            + struct.pack(">H", jbd.checksum(inner)) + bytes([jbd.END]))


def basic_frame(volts=13.37, amps=7.21, remaining=169.08, design=320.0,
                cycles=85, soc=53, cells=4, temps=(23.2,), fets=0x03,
                protection=0x0000):
    """Register 0x03, built from values rather than bytes."""
    body = bytearray(23 + len(temps) * 2)
    struct.pack_into(">H", body, 0, int(round(volts * 100)))
    struct.pack_into(">h", body, 2, int(round(amps * 100)))
    struct.pack_into(">H", body, 4, int(round(remaining * 100)))
    struct.pack_into(">H", body, 6, int(round(design * 100)))
    struct.pack_into(">H", body, 8, cycles)
    struct.pack_into(">H", body, 16, protection)
    body[19] = soc
    body[20] = fets
    body[21] = cells
    body[22] = len(temps)
    for index, celsius in enumerate(temps):
        struct.pack_into(">H", body, 23 + index * 2,
                         int(round(celsius * 10)) + jbd.KELVIN_TENTHS_AT_ZERO_C)
    return frame(jbd.BASIC, body)


def cells_frame(millivolts):
    """Register 0x04, two bytes per cell."""
    body = b"".join(struct.pack(">H", mv) for mv in millivolts)
    return frame(jbd.CELLS, body)


def hardware_frame(text=REAL_HARDWARE):
    return frame(jbd.HARDWARE, text.encode("ascii"))


def corrupt(data):
    """A frame that will fail its checksum."""
    broken = bytearray(data)
    broken[-2] ^= 0xFF
    return bytes(broken)
