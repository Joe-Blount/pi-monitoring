"""Victron VE.Direct text protocol.

19200 8N1, read only. The controller transmits unprompted about once a second
and is never written to, so nothing here can change a charge setting.

A frame is a run of ``label<TAB>value<CR><LF>`` lines ending with a Checksum
field. It is valid when every byte in it, separators and the checksum byte
included, sums to zero modulo 256.

The frame is parsed byte-wise rather than by lines. The checksum value is a
single raw byte which can itself be a carriage return, line feed or tab, so
reading by lines splits a frame apart roughly once every 85 frames. That is
frequent enough to look like an intermittent hardware fault and rare enough to
survive a casual test, which is why it has its own fixtures.

The ``I`` field is the charger's own output current, not net battery current.
It reports production, reads zero at night while loads are running, and cannot
be used to measure consumption. Consumption needs a shunt.
"""

import time

from ..duration import seconds as _seconds
from .base import Driver, DriverError, required

DEFAULT_BAUD = 19200

CHARGE_STATE = {
    0: "off", 2: "fault", 3: "bulk", 4: "absorption", 5: "float",
    6: "storage", 7: "equalize_manual", 245: "starting_up",
    246: "repeated_absorption", 247: "auto_equalize", 248: "battery_safe",
    252: "external_control",
}

TRACKER = {0: "off", 1: "limited", 2: "mppt_active"}


#: A real frame is about 200 bytes. Anything far past that means the stream is
#: not VE.Direct at all: the wrong baud rate, or a noisy line, where stray tabs
#: and newlines make fields that never end in a Checksum label.
MAX_FRAME_BYTES = 1024


def frames(source, stop_when_empty=False, idle_limit=None, clock=time.time,
           max_frame_bytes=MAX_FRAME_BYTES):
    """Yield ``(fields, checksum_ok)`` for each frame read from `source`.

    `source` is anything with ``read(1)`` returning bytes: a serial port in
    production, a byte stream in tests. A serial port returns an empty result
    on timeout, which is normal and simply means wait; a test stream returns
    empty at the end of its data, which is why `stop_when_empty` exists.

    `idle_limit` is the number of seconds of silence after which this gives up
    rather than waiting forever. Without it a connected cable with a dark
    controller, or a by-id path that resolves to a different adapter, produces
    a reader that blocks indefinitely: a check that never returns, an install
    script that stops with no message, and a long-lived child that emits
    nothing and never exits, so nothing restarts it.

    A frame that grows past `max_frame_bytes` is abandoned and reported as
    invalid. Otherwise a stream that is not VE.Direct accumulates fields
    without limit, silently, on a machine with a few hundred megabytes.
    """
    fields = {}
    total = 0
    label = bytearray()
    value = bytearray()
    in_value = False
    frame_bytes = 0
    last_progress = clock()

    def restart():
        return {}, 0, bytearray(), bytearray(), False, 0

    while True:
        byte = source.read(1)
        if not byte:
            if stop_when_empty:
                return
            if idle_limit is not None and clock() - last_progress > idle_limit:
                raise DriverError(
                    "no complete frame in %gs. The controller may be off, or "
                    "the port may not be the one it is on" % idle_limit)
            continue

        last_progress = clock()
        total = (total + byte[0]) % 256
        frame_bytes += 1
        if frame_bytes > max_frame_bytes:
            yield ({k.decode("ascii", "replace"): v.decode("ascii", "replace")
                    for k, v in fields.items()}, False)
            fields, total, label, value, in_value, frame_bytes = restart()
            continue

        if not in_value:
            if byte == b"\t":
                in_value = True
            elif byte in (b"\r", b"\n"):
                label.clear()
            else:
                label += byte
            continue

        if label == b"Checksum":
            # The checksum value is exactly one raw byte, already counted
            # above. Whatever it happens to be, including CR, LF or TAB, it
            # ends the frame.
            yield (
                {k.decode("ascii", "replace"): v.decode("ascii", "replace")
                 for k, v in fields.items()},
                total == 0,
            )
            fields, total, in_value = {}, 0, False
            frame_bytes = 0
            label.clear()
            value.clear()
            continue

        if byte == b"\n":
            fields[bytes(label)] = bytes(value)
            label.clear()
            value.clear()
            in_value = False
        elif byte != b"\r":
            value += byte


def normalize(raw):
    """Turn the raw strings of one frame into useful units."""

    def whole(key):
        try:
            return int(raw[key])
        except (KeyError, TypeError, ValueError):
            return None

    def scaled(key, factor):
        value = whole(key)
        return None if value is None else round(value * factor, 3)

    out = {
        "battery_volts": scaled("V", 0.001),
        "battery_amps": scaled("I", 0.001),
        "pv_volts": scaled("VPV", 0.001),
        "pv_watts": whole("PPV"),
        "charge_state": CHARGE_STATE.get(whole("CS"), "cs_%s" % raw.get("CS")),
        "tracker": TRACKER.get(whole("MPPT"), "mppt_%s" % raw.get("MPPT")),
        "error": whole("ERR"),
        "yield_total_kwh": scaled("H19", 0.01),
        "yield_today_kwh": scaled("H20", 0.01),
        "max_power_today_watts": whole("H21"),
        "yield_yesterday_kwh": scaled("H22", 0.01),
        "max_power_yesterday_watts": whole("H23"),
        "day_sequence": whole("HSDS"),
    }

    # OR is a bitmask the controller reports in hex. Keep the hex so it can be
    # read against Victron's table, and the number so it can be alerted on.
    off_reason = raw.get("OR")
    if off_reason:
        out["off_reason"] = off_reason
        if off_reason.lower().startswith("0x"):
            try:
                out["off_reason_code"] = int(off_reason, 16)
            except ValueError:
                pass

    if out["battery_volts"] is not None and out["battery_amps"] is not None:
        out["battery_watts"] = round(out["battery_volts"] * out["battery_amps"], 1)

    # Panel current is not reported: there is no IL or IPV field. Derive it,
    # guarded against the night, when the array sits at zero volts and a plain
    # division would produce a value InfluxDB cannot store.
    if out["pv_volts"] is not None and out["pv_watts"] is not None:
        out["pv_amps"] = (
            round(out["pv_watts"] / out["pv_volts"], 2) if out["pv_volts"] > 0 else 0.0
        )

    return out


def identity(raw):
    """The fields that describe the controller rather than its state."""
    return {key: raw[key] for key in ("PID", "FW", "SER#") if key in raw}


class VedirectDriver(Driver):
    """Read a Victron charge controller over its VE.Direct cable."""

    description = "Victron VE.Direct charge controller"
    modes = ("resident", "controller", "poll")

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        self.port = required(self.params, "port", name)
        self.baud = int(self.params.get("baud", DEFAULT_BAUD))
        self.read_timeout = float(self.params.get("read_timeout", 2))
        # Below telegraf's exec timeout on purpose. At or above it, a silent
        # controller makes the whole poll run time out, and telegraf then
        # discards every other device's output for that interval.
        self.frame_timeout = float(self.params.get("frame_timeout", 12))
        self._serial = None

    def _open(self):
        """Open the port, lazily, so importing this module needs no hardware."""
        if self._serial is not None:
            return self._serial
        try:
            import serial
        except ImportError:
            raise DriverError(
                "the vedirect driver needs pyserial (apt install python3-serial)")
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

    def read(self):
        """Return the first checksum-valid frame, or fail saying why.

        A frame failing its checksum is not an error on its own: the line is
        noisy and the controller simply sends another. Running out of time
        waiting for a good one is an error.
        """
        deadline = time.time() + self.frame_timeout
        bad = 0
        for raw, ok in frames(self._open(), idle_limit=self.frame_timeout):
            if ok:
                return normalize(raw)
            bad += 1
            if time.time() > deadline:
                raise DriverError(
                    "no valid frame from %s in %gs (%d failed the checksum)"
                    % (self.port, self.frame_timeout, bad))
        raise DriverError("the port %s stopped sending" % self.port)

    def stream(self):
        """Yield readings as frames arrive, thinned to the wanted rate.

        Frames arrive about once a second. Publishing every one would store
        thirty times the data for no extra information, so only the newest
        frame in each window is emitted. Thinning happens here rather than by
        sleeping, because the port must keep being drained: a serial buffer
        that is not read fills up and the frames that come back afterwards are
        stale and truncated.

        Frames that fail their checksum are counted and dropped. A noisy line
        is normal and the controller simply sends another; only a long run of
        them is worth reporting.
        """
        emit_every = _seconds(self.params.get("emit_every", 30))

        last_emit = 0.0
        since_good = 0
        for raw, ok in frames(self._open(), idle_limit=self.frame_timeout):
            if not ok:
                since_good += 1
                if since_good and since_good % 100 == 0:
                    raise DriverError(
                        "%d consecutive frames failed the checksum on %s"
                        % (since_good, self.port))
                continue
            since_good = 0

            now = time.time()
            if now - last_emit < emit_every:
                continue
            last_emit = now
            yield normalize(raw)

    def check(self):
        """Confirm the controller is there and talking, without a full read."""
        limit = min(self.frame_timeout, 10)
        deadline = time.time() + limit
        seen = 0
        for raw, ok in frames(self._open(), idle_limit=limit):
            seen += 1
            if ok:
                bits = identity(raw)
                detail = ", ".join("%s=%s" % kv for kv in sorted(bits.items()))
                return "frame received%s" % (" (%s)" % detail if detail else "")
            if time.time() > deadline:
                break
        raise DriverError(
            "%s: %d frame(s) seen, none valid. Check the cable and that nothing"
            " else holds the port" % (self.port, seen))

    def raw(self):
        """Return one frame's unparsed fields, for bringing up a new cable."""
        for fields, ok in frames(self._open(), idle_limit=self.frame_timeout):
            return {"checksum_ok": ok, "fields": fields}
        raise DriverError("no data from %s" % self.port)

    def close(self):
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
