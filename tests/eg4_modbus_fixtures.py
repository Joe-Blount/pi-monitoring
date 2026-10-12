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

#: Array reconnected and settled, battery recharging hard. 55.3 V at 45.5 A
#: is 2516 W and the register reports exactly 2516. Solar reads 180.7 V at
#: 14.1 A, which is 2548 W against 2554 reported, and the whole system
#: balances: 2554 W of solar against 2516 W into the battery and 73 W of
#: load, leaving 35 W, which is conversion loss.
#:
#: Settled matters. The first sample after the array was reconnected did not
#: balance, by 232 W, because the registers had not caught up with each
#: other while the charge controller was still ramping. A fixture taken
#: during a transient teaches a parser that the arithmetic need not hold.
PV_ON_RECHARGING = {
    205: 1200, 206: 15, 207: 6001, 208: 73, 213: 83, 214: 120,
    215: 553, 216: 455, 217: 2516, 219: 1807, 220: 141, 223: 2554,
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
