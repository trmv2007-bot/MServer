"""System logging for the vOS.

`/var/log` existed as an empty directory for the whole life of the project.
Tier 0 started filling `agent.log`; this adds the system side, so the vOS
keeps a record of its own operation the way a real one does:

    /var/log/syslog     general system events (boot, services, packages)
    /var/log/auth.log   dashboard authentication attempts
    /var/log/<svc>.log  per-service logs, written when a service starts/stops
    /var/log/boot.log   the most recent boot sequence

Plus a kernel ring buffer for `dmesg`, which lives in memory and is rebuilt
on every boot — as a real one is.

Logs are size-capped and rotated in place. A phone has finite storage and
nobody is going to run logrotate in here.
"""
from __future__ import annotations

import time

MAX_LOG_BYTES = 256_000
KEEP_LINES = 1500
RING_SIZE = 200


def _stamp() -> str:
    return time.strftime("%b %d %H:%M:%S")


class SysLog:
    """Writes to /var/log inside the vOS and keeps a dmesg ring buffer."""

    def __init__(self, vos, hostname: str = "mserver"):
        self.vos = vos
        self.hostname = hostname
        self.ring: list[tuple[float, str, str]] = []  # (ts, level, message)

    # ------------------------------------------------------------ dmesg
    def kernel(self, message: str, level: str = "info") -> None:
        """Append to the in-memory kernel ring buffer (dmesg)."""
        self.ring.append((time.time(), level, message))
        if len(self.ring) > RING_SIZE:
            self.ring = self.ring[-RING_SIZE:]

    def dmesg(self, boot_time: float) -> str:
        out = []
        for ts, level, msg in self.ring:
            offset = max(0.0, ts - boot_time)
            prefix = "" if level == "info" else f"{level}: "
            out.append(f"[{offset:>10.6f}] {prefix}{msg}")
        return "\n".join(out)

    def clear_ring(self) -> None:
        self.ring.clear()

    # -------------------------------------------------------------- files
    def write(self, facility: str, message: str, log: str = "syslog") -> None:
        line = f"{_stamp()} {self.hostname} {facility}: {message}\n"
        self._append(f"/var/log/{log}", line)

    def auth(self, message: str) -> None:
        self.write("mserver-dashboard", message, log="auth.log")

    def service(self, name: str, message: str) -> None:
        """Log to both syslog and the service's own file."""
        self.write(name, message)
        self._append(f"/var/log/{name}.log",
                     f"{_stamp()} {self.hostname} {name}: {message}\n")

    def boot(self, lines: list[str]) -> None:
        body = "".join(f"{_stamp()} {self.hostname} kernel: {ln}\n" for ln in lines)
        try:
            self.vos.write("/var/log/boot.log", body)
        except Exception:
            pass

    def _append(self, path: str, line: str) -> None:
        # Logging must never be able to break the thing it is logging.
        try:
            p = self.vos.vpath(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > MAX_LOG_BYTES:
                self._rotate(p)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass

    def _rotate(self, p) -> None:
        try:
            lines = p.read_text("utf-8", "replace").splitlines(keepends=True)
            p.write_text("".join(lines[-KEEP_LINES // 2:]), encoding="utf-8")
        except OSError:
            pass


BOOT_SEQUENCE = (
    "Booting MServerOS virtual kernel",
    "vos: sandboxed rootfs mounted read-write",
    "proc: synthetic /proc filesystem registered",
    "vos-pkg: package database loaded",
    "init: entering runlevel 3",
)
