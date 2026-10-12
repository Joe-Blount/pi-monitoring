"""Register blocks captured from an EG4 all-in-one, one per system state.

Taken while deliberately changing what the inverter was doing: running a
microwave, disconnecting the array, running a compressor off the battery,
then reconnecting the array. The states are what make these worth keeping,
because a register that is only ever read at idle proves nothing.
"""

from monitoring.drivers import eg4_modbus

#: Register number to raw value, as function 3 returns them.
PV_ON_IDLE = {
    205: 1199, 206: 16, 207: 6001, 208: 83, 213: 104, 214: 143,
    215: 570, 216: 0, 217: 0, 219: 2198, 220: 6, 223: 152,
}

#: Array disconnected, only the house load running. Battery discharging
#: gently: 56.9 V at 2.1 A is 119 W, and the unit reported 79 W to the
#: battery register and 94 W of load.
PV_OFF_IDLE = {
    205: 1199, 206: 16, 207: 6001, 208: 94, 213: 114, 214: 131,
    215: 569, 216: 65515, 217: 65457, 219: 152, 220: 0, 223: 0,
}

#: Array disconnected, air compressor running. The cleanest sample taken:
#: 56.2 V at 25.2 A is 1416.2 W and the unit reported exactly 1416 W, while
#: the load drew 1398 W.
PV_OFF_COMPRESSOR = {
    205: 1201, 206: 123, 207: 6001, 208: 1398, 213: 1348, 214: 1453,
    215: 562, 216: 65284, 217: 64120, 219: 296, 220: 0, 223: 0,
}

#: Array reconnected, battery recharging. 180.2 V at 14.2 A is 2559 W of
#: solar, against 2561 W reported.
PV_ON_RECHARGING = {
    205: 1179, 206: 14, 207: 6001, 208: 52, 213: 119, 214: 106,
    215: 537, 216: 378, 217: 1954, 219: 2051, 220: 86, 223: 1774,
}

ALL = {
    "pv on, idle": PV_ON_IDLE,
    "pv off, idle": PV_OFF_IDLE,
    "pv off, compressor": PV_OFF_COMPRESSOR,
    "pv on, recharging": PV_ON_RECHARGING,
}


def reply(registers, unit=1, start=eg4_modbus.BLOCK_START,
          count=eg4_modbus.BLOCK_COUNT):
    """Build the bytes a unit would send back for this block."""
    body = b"".join(
        int(registers.get(start + i, 0)).to_bytes(2, "big")
        for i in range(count))
    head = bytes([unit, eg4_modbus.READ_HOLDING, len(body)]) + body
    return head + eg4_modbus.crc16(head)


class FakeTransport:
    """Returns prepared replies, with no serial library involved."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.closed = False

    def exchange(self, frame):
        self.sent.append(frame)
        return self.replies.pop(0) if self.replies else b""

    def close(self):
        self.closed = True
