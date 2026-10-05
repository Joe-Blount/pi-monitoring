"""Voltronic PI30 protocol, as used by EG4 all-in-one inverters.

The inverter answers short ASCII queries. A query is the command text, then a
two byte CRC, then a carriage return. A reply opens with ``(``, carries
space-separated values, and closes with its own CRC and a carriage return.

Three details decide whether this works at all, and all three have already
cost time:

The CRC is CRC-16/XMODEM, and any CRC byte that would come out as ``(``,
carriage return or line feed is incremented by one. That is a quirk of the
protocol rather than of the checksum, and a strictly correct CRC
implementation therefore fails.

The port is the one printed RS232 or COM, at 2400 baud. Not the jack printed
RS485, which is the battery channel where the inverter is the master and never
answers a query. Not the USB-B port either, which is documented as unusable
for monitoring on this model.

Values arrive by position, not by name. Firmware versions differ in how many
they send, so a short reply is parsed as far as it goes rather than discarded.
"""

import sys

from .base import Driver, DriverError, required

DEFAULT_BAUD = 2400

#: Bytes a CRC may not contain, because they are framing characters. The
#: protocol increments them rather than escaping them.
RESERVED = (0x28, 0x0D, 0x0A)

#: QPIGS values in the order the inverter sends them. Each entry is the field
#: name, the factor to apply, and whether it is published at all.
QPIGS_FIELDS = (
    ("grid_volts", 1.0),
    ("grid_hz", 1.0),
    ("ac_out_volts", 1.0),
    ("ac_out_hz", 1.0),
    ("ac_out_va", 1.0),
    ("ac_out_watts", 1.0),
    ("load_percent", 1.0),
    ("bus_volts", 1.0),
    ("battery_volts", 1.0),
    ("battery_charge_amps", 1.0),
    ("battery_percent", 1.0),
    ("heatsink_temp_c", 1.0),       # converted to Fahrenheit below
    ("pv_amps", 1.0),
    ("pv_volts", 1.0),
    ("scc_battery_volts", 1.0),
    ("battery_discharge_amps", 1.0),
    (None, None),                   # device status bits, handled separately
    ("fan_on_offset_volts", 0.01),
    ("eeprom_version", 1.0),
    ("pv_watts", 1.0),
)

#: The eight device status bits, most significant first, and the ones worth
#: publishing. The rest are firmware housekeeping.
STATUS_BITS = (
    None,                 # b7  add SBU priority version
    None,                 # b6  configuration changed
    None,                 # b5  charge controller firmware updated
    "load_on",            # b4
    None,                 # b3  battery voltage steady while charging
    "charging_on",        # b2
    "scc_charging_on",    # b1
    "ac_charging_on",     # b0
)

MODES = {
    "P": "power_on", "S": "standby", "L": "line", "B": "battery",
    "F": "fault", "H": "power_saving", "D": "shutdown", "Y": "bypass",
}


def crc16(data):
    """CRC-16/XMODEM over `data`."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def crc_bytes(data):
    """The two CRC bytes the protocol appends, with its reserved-byte quirk."""
    value = crc16(data)
    return bytes(
        (byte + 1) if byte in RESERVED else byte
        for byte in (value >> 8, value & 0xFF)
    )


def frame(command):
    """Build one query: the command, its CRC, and a carriage return."""
    body = command.encode("ascii")
    return body + crc_bytes(body) + b"\r"


def reply_is_valid(reply):
    """Whether a reply's own CRC matches its contents.

    Returned rather than raised, so a caller can choose to carry on. Some
    firmware revisions compute this differently from the specification, and
    refusing every reading on that basis would be worse than reporting it.
    """
    body = reply[:-1] if reply.endswith(b"\r") else reply
    if len(body) < 3:
        return False
    # The checksum covers everything before itself, the opening marker
    # included, and the carriage return is a terminator rather than part of
    # the message.
    return crc_bytes(body[:-2]) == body[-2:]


def strip(reply):
    """Return a reply's payload: no leading marker, no CRC, no terminator."""
    body = reply
    if body.endswith(b"\r"):
        body = body[:-1]
    if len(body) >= 2:
        body = body[:-2]
    if body.startswith(b"("):
        body = body[1:]
    return body.decode("ascii", "replace").strip()


def parse_status_bits(text):
    """Turn the eight device status characters into named values."""
    out = {}
    for position, name in enumerate(STATUS_BITS):
        if name is None or position >= len(text):
            continue
        out[name] = 1.0 if text[position] == "1" else 0.0
    return out


def parse_qpigs(payload):
    """Parse a general status reply.

    Parsed as far as the values go. Firmware revisions send different numbers
    of them, and discarding a whole reading because the last field is absent
    would lose the fifteen that did arrive.
    """
    parts = payload.split()
    if not parts:
        raise DriverError("the inverter returned an empty status reply")

    out = {}
    for index, part in enumerate(parts):
        if index >= len(QPIGS_FIELDS):
            break
        name, factor = QPIGS_FIELDS[index]
        if name is None:
            out.update(parse_status_bits(part))
            continue
        try:
            out[name] = float(part) * factor
        except ValueError:
            # One unreadable value must not cost the others.
            continue

    celsius = out.pop("heatsink_temp_c", None)
    if celsius is not None:
        out["heatsink_temp_f"] = round(celsius * 9.0 / 5.0 + 32.0, 1)

    return out


def parse_mode(payload):
    """Parse an operating mode reply."""
    letter = payload.strip()[:1]
    return {"mode": MODES.get(letter, "mode_%s" % letter)} if letter else {}


class Pi30Driver(Driver):
    """Query a Voltronic or EG4 inverter over its RS232 port."""

    description = "Voltronic PI30 inverter"

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.port = required(self.params, "port", name)
        self.baud = int(self.params.get("baud", DEFAULT_BAUD))
        self.read_timeout = float(self.params.get("read_timeout", 5))
        self.queries = list(self.params.get("queries", ["QPIGS", "QMOD"]))
        # Strict by default, because a wrong reading is worse than none. The
        # message says how to relax it, since some firmware disagrees with the
        # specification here.
        self.verify_crc = self.params.get("verify_reply_crc", True)
        self._serial = None

    def _open(self):
        if self._serial is not None:
            return self._serial
        try:
            import serial
        except ImportError:
            raise DriverError(
                "the pi30 driver needs pyserial (apt install python3-serial)")
        try:
            # Exclusive: if telegraf already holds this port, a manual run
            # must fail saying so rather than both readers seeing torn frames,
            # which looks exactly like a cable fault.
            self._serial = serial.Serial(self.port, self.baud,
                                         timeout=self.read_timeout,
                                         exclusive=True)
        except Exception as exc:
            raise DriverError("cannot open %s: %s" % (self.port, exc))
        return self._serial

    def ask(self, command):
        """Send one query and return the raw reply, terminator included."""
        port = self._open()
        try:
            port.reset_input_buffer()
        except Exception:
            pass
        port.write(frame(command))
        reply = port.read_until(b"\r")
        if not reply:
            raise DriverError(
                "no reply to %s from %s. Check this is the port printed RS232 "
                "or COM rather than the RS485 battery jack, and that the baud "
                "rate is %d" % (command, self.port, self.baud))
        if not reply.endswith(b"\r"):
            # read_until returns what it has when it times out. Without this
            # check, strip() takes the last two payload characters for a
            # checksum and the remainder parses as perfectly ordinary values.
            raise DriverError(
                "incomplete reply to %s (%d bytes, no terminator). The baud "
                "rate or the cable is wrong" % (command, len(reply)))
        if reply.startswith(b"(NAK"):
            raise DriverError("the inverter rejected %s as unsupported" % command)
        if self.verify_crc and not reply_is_valid(reply):
            raise DriverError(
                "the reply to %s failed its checksum. If this inverter's "
                "firmware computes it differently, set verify_reply_crc: false "
                "on this device" % command)
        return reply

    def read(self):
        """Query the inverter, reporting any query that failed.

        A failure is reported even when another query succeeded. Otherwise a
        status query failing every interval while the mode query answers looks
        like a working inverter that has stopped reporting its battery.
        """
        fields = {}
        failures = []
        for command in self.queries:
            try:
                payload = strip(self.ask(command))
            except DriverError as exc:
                failures.append("%s: %s" % (command, exc))
                continue
            if command == "QPIGS":
                fields.update(parse_qpigs(payload))
            elif command == "QMOD":
                fields.update(parse_mode(payload))
            else:
                fields["%s_raw" % command.lower()] = payload

        if not fields:
            raise DriverError("; ".join(failures) or "the inverter returned nothing")
        if failures:
            # Partial data is still worth publishing, but the gap must not be
            # silent. The runner turns this into a visible point.
            sys.stderr.write("inverter: %s\n" % "; ".join(failures))
        return fields

    def check(self):
        payload = strip(self.ask("QPI"))
        return "inverter answering, protocol id %s" % (payload or "unknown")

    def raw(self):
        return {command: strip(self.ask(command)) for command in self.queries}

    def close(self):
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
