"""Report the health of the machine doing the monitoring.

Deliberately the first driver written. It needs no wiring, so it proves the
whole chain end to end -- node file, runner, line protocol, telegraf, InfluxDB
-- before any sensor is involved. When something later goes wrong, this is
also the driver that says whether the machine itself is the problem.

Every path it reads is injectable, so the whole thing is testable on a laptop
against a fixture tree rather than only on Linux.
"""

import os
import socket

from .base import Driver, DriverError


class HostDriver(Driver):
    """CPU temperature, uptime, boot time, root filesystem and address."""

    description = "machine health: temperature, uptime, disk, address"

    def __init__(self, name, params, tags):
        Driver.__init__(self, name, params, tags)
        # Pointing these at a fixture tree is what makes this testable off a
        # Raspberry Pi. In production they are the real paths.
        self.sysfs_root = self.params.get("sysfs_root", "/")
        self.filesystem = self.params.get("filesystem", "/")
        self.report_address = self.params.get("report_address", True)

    # -- helpers ----------------------------------------------------------

    def _path(self, relative):
        return os.path.join(self.sysfs_root, relative.lstrip("/"))

    def _first_line(self, relative):
        try:
            with open(self._path(relative)) as handle:
                return handle.readline().strip()
        except OSError:
            return None

    # -- individual readings ----------------------------------------------

    def cpu_temp_f(self):
        """Chip temperature in Fahrenheit.

        Fahrenheit because every temperature in this project is Fahrenheit,
        converted in the driver. This is the chip, not the air around it, and
        it is the one that throttles.
        """
        raw = self._first_line("sys/class/thermal/thermal_zone0/temp")
        try:
            return round(int(raw) / 1000.0 * 9.0 / 5.0 + 32.0, 1)
        except (TypeError, ValueError):
            return None

    def boot_time(self):
        """Epoch seconds of the last boot, so a reboot shows as a step."""
        try:
            with open(self._path("proc/stat")) as handle:
                for line in handle:
                    if line.startswith("btime"):
                        return float(line.split()[1])
        except (OSError, IndexError, ValueError):
            pass
        return None

    def uptime_seconds(self):
        """Seconds since boot. Graphs as a sawtooth, so resets stand out."""
        raw = self._first_line("proc/uptime")
        try:
            return round(float(raw.split()[0]), 1)
        except (AttributeError, IndexError, ValueError):
            return None

    def root_filesystem(self):
        """Free gigabytes and percent used, as insurance against a full card.

        Percent used is computed the way df does it, against the space a
        normal user can actually reach. That excludes the filesystem's
        reserved blocks; dividing by the raw total instead reads several
        points higher than df and makes the two disagree for no good reason.
        """
        try:
            stat = os.statvfs(self.filesystem)
        except OSError:
            return None, None
        free = stat.f_bavail * stat.f_frsize
        used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
        if used + free <= 0:
            return None, None
        return round(free / 1e9, 2), round(100.0 * used / (used + free), 1)

    def address(self):
        """The address of whichever interface holds the default route.

        Opening a datagram socket sends nothing. It only makes the kernel
        choose a source address, which is the one a reply would come back to.
        Published so the machine can be reached without hunting for it, which
        matters when a lease changes at a site an hour away.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
        except OSError:
            return None

    # -- driver interface -------------------------------------------------

    def read(self):
        free_gb, used_pct = self.root_filesystem()
        fields = {
            "cpu_temp_f": self.cpu_temp_f(),
            "boot_time": self.boot_time(),
            "uptime_seconds": self.uptime_seconds(),
            "root_free_gb": free_gb,
            "root_used_pct": used_pct,
        }
        if self.report_address:
            fields["ip"] = self.address()

        if not any(value is not None for value in fields.values()):
            raise DriverError(
                "no host readings available under %r" % self.sysfs_root
            )
        return fields

    def check(self):
        missing = [
            name for name, relative in (
                ("uptime", "proc/uptime"),
                ("boot time", "proc/stat"),
            ) if self._first_line(relative) is None
        ]
        if missing:
            raise DriverError(
                "cannot read %s under %r" % (" or ".join(missing), self.sysfs_root)
            )
        temp = self.cpu_temp_f()
        return "uptime readable, temperature %s" % (
            "%.1f F" % temp if temp is not None else "not exposed"
        )
