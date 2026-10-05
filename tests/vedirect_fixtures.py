"""Build VE.Direct frames with correct checksums.

Generating frames rather than storing a capture lets a test ask for the exact
awkward case it wants, in particular a frame whose checksum byte happens to be
a carriage return, line feed or tab. Those are the frames that break a parser
reading by lines, they occur about once every 85 frames on real hardware, and
they are close to impossible to capture on demand.
"""


def frame(fields, pad_label="PAD"):
    """Return the bytes of one valid frame carrying `fields`.

    `fields` is an ordered mapping of label to value, both strings. The
    checksum byte is whatever makes every byte in the frame sum to zero
    modulo 256.
    """
    body = bytearray()
    for label, value in fields.items():
        body += b"\r\n" + label.encode() + b"\t" + str(value).encode()
    body += b"\r\nChecksum\t"
    checksum = (256 - (sum(body) % 256)) % 256
    return bytes(body) + bytes([checksum])


def frame_with_checksum_byte(fields, wanted, pad_label="PAD"):
    """Return a valid frame whose checksum byte is exactly `wanted`.

    Pads a filler field until the arithmetic lands on the wanted byte, so a
    test can demand the carriage return case rather than wait for luck.

    The padding character must have an odd byte value. Each added character
    shifts the checksum by that value modulo 256, so an even one can only
    reach a fraction of the possible bytes: padding with "x" at 120 shares a
    factor of eight with 256 and reaches one value in eight.
    """
    pad_char = "a"              # 0x61, odd, so every byte value is reachable
    assert ord(pad_char) % 2 == 1
    for length in range(0, 257):
        padded = dict(fields)
        padded[pad_label] = pad_char * length
        candidate = frame(padded)
        if candidate[-1] == wanted:
            return candidate
    raise AssertionError("no padding produced checksum byte %r" % wanted)


def corrupt(frame_bytes):
    """Return a frame that will fail its checksum."""
    broken = bytearray(frame_bytes)
    broken[-1] = (broken[-1] + 1) % 256
    return bytes(broken)


#: A frame shaped like the controller at the deer blind on a sunny afternoon.
SUNNY = {
    "PID": "0xA057",
    "FW": "159",
    "SER#": "HQ25453MGTX",
    "V": "13510",        # millivolts
    "I": "21800",        # milliamps, positive when charging
    "VPV": "33700",      # millivolts
    "PPV": "300",        # watts
    "CS": "3",           # bulk
    "MPPT": "2",         # tracking
    "OR": "0x00000000",
    "ERR": "0",
    "H19": "9055",       # 0.01 kWh
    "H20": "171",
    "H21": "659",
    "H22": "324",
    "H23": "561",
    "HSDS": "12",
}

#: The same controller at night: no production, array at zero volts.
NIGHT = dict(SUNNY, V="12800", I="0", VPV="0", PPV="0", CS="0",
             MPPT="0", OR="0x00000001", H20="0", H21="0")
