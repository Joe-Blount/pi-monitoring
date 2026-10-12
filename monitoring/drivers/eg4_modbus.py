"""EG4 all-in-one inverters that speak Modbus RTU over RS232.

Named for the brand, unusually for this project, because there is no
protocol name to use instead. The transport is ordinary Modbus RTU, which
says nothing about what any register means, and the register map below is
the vendor's. These units are rebadged, so the same map may well appear
under other names; if one turns up, rename this and keep the map.

Not to be confused with the pi30 driver. That reads the Voltronic ASCII
protocol at 2400 baud, which many EG4 units do speak. This one does not: it
answers nothing at all to PI30 at any baud, and answers Modbus at 9600
immediately. Check which one a unit speaks before assuming, because a unit
that ignores PI30 looks exactly like a dead cable.

    Modbus RTU, 9600 8N1, unit 1, function 3 (read holding registers)

How the map was established
---------------------------
Nothing here was guessed. The map came from watching the registers while
known things happened, and every published field is confirmed by at least
one independent reading:

* Battery voltage was read against two battery packs on the same bus,
  measured over Bluetooth at the same moment.
* The sign of battery current was proved by disconnecting the array and
  running a large load, then reconnecting it. The register goes negative
  and then positive, rather than being assumed either way.
* PV voltage and current multiply out to PV power, to within a tenth of a
  percent.
* Battery volts times battery amps equals battery watts to the watt: at
  56.2 V and 25.2 A the unit reported 1416 W.
* The whole system balances. With the array off and a compressor running,
  the battery supplied 1416 W and the load drew 1398 W.

Those last three are checked again on every read, in parse(), because they
are the cheapest way to catch a register map that has shifted under a
firmware update.
"""

import sys
import time

from .base import Driver, DriverError, required

DEFAULT_BAUD = 9600
DEFAULT_UNIT = 1

#: Read holding registers. The only function this driver sends. Nothing here
#: writes, and the settings registers nearby include charge voltages that a
#: wrong write would quietly change.
READ_HOLDING = 3

#: One contiguous block covers everything worth having, so a reading is one
#: transaction and cannot be half old and half new.
BLOCK_START = 205
BLOCK_COUNT = 19


class Field:
    """One value inside the register block.

    Declared rather than coded, so correcting a register against real
    hardware is a one-line change instead of surgery on a parser.
    """

    def __init__(self, name, register, scale=1.0, signed=False,
                 low=None, high=None):
        self.name = name
        self.register = register
        self.scale = scale
        self.signed = signed
        self.low = low
        self.high = high

    def read(self, registers):
        raw = registers.get(self.register)
        if raw is None:
            return None
        if self.signed and raw > 0x7FFF:
            raw -= 0x10000
        # Always a float. InfluxDB refuses a field whose type changes between
        # writes, so a whole-number scale must not publish an integer the
        # first time it happens to read one.
        return float(raw * self.scale)

    def plausible(self, value):
        if value is None:
            return False
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value > self.high:
            return False
        return True


#: Bounds are wide on purpose. They catch a register that moved, not an
#: inverter having a bad day.
FIELDS = (
    Field("ac_out_volts", 205, 0.1, low=80.0, high=150.0),
    Field("ac_out_amps", 206, 0.1, low=0.0, high=60.0),
    Field("ac_out_hz", 207, 0.01, low=50.0, high=70.0),
    # Register 213 carries a second, slightly different load figure that has
    # not been identified. 208 is the one that balances against battery
    # power, so it is the one published.
    Field("load_watts", 208, 1.0, low=0.0, high=8000.0),
    Field("load_va", 214, 1.0, low=0.0, high=8000.0),
    Field("battery_volts", 215, 0.1, low=30.0, high=70.0),
    Field("battery_amps", 216, 0.1, signed=True, low=-400.0, high=400.0),
    Field("battery_watts", 217, 1.0, signed=True, low=-8000.0, high=8000.0),
    Field("pv_volts", 219, 0.1, low=0.0, high=600.0),
    Field("pv_amps", 220, 0.1, low=0.0, high=40.0),
    Field("pv_watts", 223, 1.0, low=0.0, high=8000.0),
)

#: How far a reported power may sit from volts times amps before the reading
#: is refused.
#:
#: Both halves are set from measurement rather than taste. At high power the
#: agreement is extraordinary: 56.2 V times 25.2 A is 1416.2 W and the unit
#: reported 1416 W. At low power it is not, because the current register has
#: 0.1 A resolution, which at 57 V is 5.7 W per step, and the readings are
#: not taken in the same instant. The worst observed disagreement was 40 W,
#: on a battery drawing about 119 W. The floor is set at twice that, so a
#: quiet system is never refused for arithmetic noise, while a map that has
#: moved misses by hundreds of watts and is still caught.
POWER_TOLERANCE_PCT = 12.0
POWER_TOLERANCE_WATTS = 80.0

#: Checked pairs: the power register, and the volts and amps that should
#: multiply out to it. This is what catches a shifted map, so it runs on
#: every read rather than only in a test.
POWER_CHECKS = (
    ("battery_watts", "battery_volts", "battery_amps"),
    ("pv_watts", "pv_volts", "pv_amps"),
)


def crc16(data):
    """The Modbus CRC, low byte first on the wire."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def request(unit, start, count):
    """Build one read. Only the read function can be built."""
    frame = bytes([unit, READ_HOLDING,
                   (start >> 8) & 0xFF, start & 0xFF,
                   (count >> 8) & 0xFF, count & 0xFF])
    return frame + crc16(frame)


def parse_reply(reply, unit, start, count):
    """Turn a reply into {register: value}, or say why it is not one."""
    if len(reply) < 5:
        return None, "short reply, %d bytes" % len(reply)
    if reply[0] != unit:
        return None, "answered for unit %d, not %d" % (reply[0], unit)
    if reply[1] & 0x80:
        return None, "exception %d" % (reply[2] if len(reply) > 2 else 0)
    if reply[1] != READ_HOLDING:
        return None, "function %d, not %d" % (reply[1], READ_HOLDING)
    if crc16(reply[:-2]) != reply[-2:]:
        return None, "checksum does not match"
    length = reply[2]
    body = reply[3:3 + length]
    if len(body) != length or length != count * 2:
        return None, "expected %d bytes of registers, got %d" % (count * 2, len(body))
    return {start + index // 2: int.from_bytes(body[index:index + 2], "big")
            for index in range(0, length, 2)}, None


def parse(registers):
    """Turn registers into published values.

    A value outside its range is dropped rather than published, and a
    reading whose own numbers contradict each other is refused entirely.
    The commonest cause of both is a register map that moved, and a wrong
    number in the database is worse than a missing one.
    """
    out = {}
    rejected = []

    for field in FIELDS:
        value = field.read(registers)
        if field.plausible(value):
            out[field.name] = value
        elif value is not None:
            rejected.append("%s=%g" % (field.name, value))

    # Voltage is the anchor. Current and power both read near zero on a
    # healthy idle system, so a block of noise leaves plausible-looking
    # numbers behind, and the reading would claim success on them. An
    # inverter that answers at all has a battery voltage.
    if "battery_volts" not in out:
        raise DriverError(
            "no plausible battery voltage, so nothing read is trusted%s" % (
                "; out of range: " + ", ".join(rejected) if rejected else ""))

    # Volts times amps must equal watts. Three registers, read independently,
    # that can only agree if all three are where this driver thinks they are.
    for watts_name, volts_name, amps_name in POWER_CHECKS:
        watts = out.get(watts_name)
        volts = out.get(volts_name)
        amps = out.get(amps_name)
        if watts is None or volts is None or amps is None:
            continue
        expected = volts * amps
        allowed = max(POWER_TOLERANCE_WATTS,
                      abs(expected) * POWER_TOLERANCE_PCT / 100.0)
        if abs(expected - watts) > allowed:
            raise DriverError(
                "%s reads %.0f W but %.1f V times %.1f A is %.0f W, so the "
                "register map does not match this firmware"
                % (watts_name, watts, volts, amps, expected))

    if rejected:
        out["rejected_fields"] = ", ".join(rejected)
    return out


class Eg4ModbusDriver(Driver):
    """An EG4 all-in-one inverter, read over RS232 as Modbus RTU."""

    description = "EG4 all-in-one inverter over RS232 (Modbus RTU)"
    modes = ("poll", "controller")

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.port = required(self.params, "port", name)
        self.baud = int(self.params.get("baud", DEFAULT_BAUD))
        self.unit = int(self.params.get("unit", DEFAULT_UNIT))
        self.read_timeout = float(self.params.get("read_timeout", 1.5))
        self.retries = int(self.params.get("retries", 2))
        # Injected by the tests. Production opens a serial port on demand, so
        # importing this module needs no serial library.
        self.transport = self.params.get("transport")

    def _registers(self):
        transport = self.transport or _SerialTransport(
            self.port, self.baud, self.read_timeout)
        try:
            last = "no attempt made"
            for attempt in range(self.retries + 1):
                reply = transport.exchange(
                    request(self.unit, BLOCK_START, BLOCK_COUNT))
                registers, reason = parse_reply(
                    reply, self.unit, BLOCK_START, BLOCK_COUNT)
                if registers is not None:
                    return registers
                last = reason
                # A line shared with nothing still picks up noise, and a
                # damaged reply is worth one more try before it is a failure.
                if attempt < self.retries:
                    sys.stderr.write("%s: %s, retrying\n" % (self.port, last))
                    time.sleep(0.2)
            raise DriverError(
                "%s did not answer usefully: %s. Check this is the port "
                "printed RS232 rather than the RS485 battery jack, that the "
                "rate is %d, and that this unit speaks Modbus rather than "
                "the PI30 ASCII protocol" % (self.port, last, self.baud))
        finally:
            if transport is not self.transport:
                transport.close()

    def read(self):
        return parse(self._registers())

    def check(self):
        registers = self._registers()
        volts = Field("battery_volts", 215, 0.1).read(registers)
        return "answering on unit %d, battery %.1f V" % (self.unit, volts)

    def raw(self):
        """Every register in the block, for confirming one against reality."""
        registers = self._registers()
        return {str(number): float(value)
                for number, value in sorted(registers.items())}


class _SerialTransport:
    """Moves bytes. Knows nothing about what they mean.

    Separated so the protocol above is testable with no serial library
    installed, which is most of the value: the decoding is where the
    mistakes are, and the cable is where the slowness is.
    """

    def __init__(self, port, baud, read_timeout):
        self.port = port
        self.baud = baud
        self.read_timeout = read_timeout
        self._serial = None

    def _open(self):
        if self._serial is not None:
            return self._serial
        try:
            import serial
        except ImportError:
            raise DriverError(
                "the eg4_modbus driver needs pyserial "
                "(apt install python3-serial)")
        try:
            self._serial = serial.Serial(
                self.port, self.baud, timeout=self.read_timeout,
                write_timeout=self.read_timeout)
        except Exception as exc:                  # noqa: BLE001
            raise DriverError("cannot open %s: %s" % (self.port, exc))
        return self._serial

    def exchange(self, frame):
        link = self._open()
        try:
            link.reset_input_buffer()
            link.write(frame)
            # Read to the timeout rather than to a byte count: a damaged or
            # truncated reply must come back as the bytes that arrived, so
            # the caller can say what was wrong with it.
            deadline = time.time() + self.read_timeout
            reply = bytearray()
            while time.time() < deadline:
                waiting = getattr(link, "in_waiting", 0)
                chunk = link.read(waiting or 1)
                if not chunk:
                    if reply:
                        break
                    continue
                reply.extend(chunk)
                # unit, function, length, the registers, then two of checksum.
                if len(reply) > 2 and len(reply) >= 5 + reply[2]:
                    break
            return bytes(reply)
        except DriverError:
            raise
        except Exception as exc:                  # noqa: BLE001
            raise DriverError("cannot read %s: %s" % (self.port, exc))

    def close(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:                     # noqa: BLE001
                pass
            self._serial = None
