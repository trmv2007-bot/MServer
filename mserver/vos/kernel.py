"""MServerOS kernel: a small virtual Linux-like OS.

The entire filesystem lives inside one real directory (the "rootfs"), so
everything the agent does is sandboxed away from the device. On top of
the files we keep a small process/service table and a few kernel stats.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .procfs import ProcFS
from .syslog import BOOT_SEQUENCE, SysLog

OS_NAME = "MServerOS"
OS_VERSION = "1.0"
SHELL_NAME = "msh"
SHELL_VERSION = "1.0"

BASE_DIRS = (
    "/bin", "/sbin", "/etc", "/home", "/opt", "/proc", "/root", "/srv",
    "/sys", "/tmp", "/usr/bin", "/usr/lib", "/usr/sbin", "/var/log",
    "/var/lib/vos", "/var/run",
)

BASE_FILES = {
    "/etc/hostname": "mserver\n",
    "/etc/os-release": (
        'NAME="MServerOS"\n'
        'VERSION=1.0\n'
        'ID=mserveros\n'
        'PRETTY_NAME="MServerOS 1.0 (virtual Linux for Termux)"\n'
    ),
    "/etc/hosts": "127.0.0.1\tmserver localhost\n::1\tmserver ip6-localhost\n",
    "/etc/passwd": "root:x:0:0:root:/root:/root/.mshrc\n",
    "/root/.mshrc": "alias ll='ls -la'\n",
    "/README.txt": (
        "Welcome to MServerOS — a tiny virtual Linux living inside Termux.\n"
        "This whole filesystem is sandboxed to the vOS data directory.\n"
        "Talk to the agent in plain English, or type any msh command directly.\n"
    ),
}


class VOSPathError(Exception):
    """A path would escape the virtual filesystem."""


class VOSFsError(Exception):
    """Ordinary filesystem failure inside the vOS."""


class VOS:
    def __init__(self, root, hostname: str = "mserver"):
        self.root = Path(root).expanduser().resolve()
        self.hostname = hostname
        self.boot_time = time.time()
        self.next_pid = 10
        self.processes: dict[int, dict] = {}
        self.services: dict[str, dict] = {}
        self.procfs = ProcFS(self)
        self.syslog = SysLog(self, hostname)
        self._boot()

    # ------------------------------------------------------------- booting
    def _boot(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for d in BASE_DIRS:
            self.vpath(d).mkdir(parents=True, exist_ok=True)
        for p, content in BASE_FILES.items():
            f = self.vpath(p)
            if not f.exists():
                f.write_text(content, encoding="utf-8")
        self.processes.clear()
        self.services.clear()
        self.add_process("init", "init: /sbin/init (MServerOS 1.0)")
        self.add_process("mserver", "mserver: termux session")
        self.add_process("mserver-agent", "mserver-agent: ai core")
        self._log_boot()
        started = self.start_enabled_services()
        for name in started:
            self.syslog.service(name, "started at boot")

    def _log_boot(self) -> None:
        self.syslog.clear_ring()
        for line in BOOT_SEQUENCE:
            self.syslog.kernel(line)
        self.syslog.boot(list(BOOT_SEQUENCE))
        self.syslog.write("kernel", f"{OS_NAME} {OS_VERSION} booted")

    def reboot(self) -> None:
        self.boot_time = time.time()
        self.processes.clear()
        self.services.clear()
        self.add_process("init", "init: /sbin/init (MServerOS 1.0)")
        self.add_process("mserver", "mserver: termux session")
        self.add_process("mserver-agent", "mserver-agent: ai core")
        self.syslog.write("kernel", "system reboot requested")
        self._log_boot()
        for name in self.start_enabled_services():
            self.syslog.service(name, "restarted after reboot")

    # ------------------------------------------------------------- services
    def _svcfile(self) -> Path:
        return self.vpath("/var/lib/vos/services.json")

    def enabled_services(self) -> dict:
        """Services marked to start at boot, as {name: {binary, cmdline}}."""
        f = self._svcfile()
        if not f.exists():
            return {}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def set_enabled_service(self, name: str, defn: dict | None) -> None:
        """Enable (defn given) or disable (None) a service across reboots."""
        data = self.enabled_services()
        if defn is None:
            data.pop(name, None)
        else:
            data[name] = defn
        f = self._svcfile()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                     encoding="utf-8")

    def start_enabled_services(self) -> list[str]:
        """Bring up every enabled service. Called on boot and reboot.

        Previously a reboot silently wiped running services, which made
        `service nginx start` feel unreliable.
        """
        started = []
        for name, defn in sorted(self.enabled_services().items()):
            if self.service_state(name) == "running":
                continue
            pid = self.add_process(defn.get("binary", name),
                                   defn.get("cmdline", f"{name}: service"),
                                   service=name)
            try:
                self.write(f"/var/run/{name}.pid", f"{pid}\n")
            except (VOSPathError, VOSFsError):
                pass
            started.append(name)
        return started

    # -------------------------------------------------------- path sandbox
    def vpath(self, p) -> Path:
        """Resolve a virtual path, refusing anything that escapes the rootfs."""
        if p is None:
            raise VOSFsError("no path given")
        p = str(p)
        if p.startswith("~"):
            p = "/root" + p[1:]
        p = "/" + p.lstrip("/")
        target = (self.root / p[1:]).resolve()
        root = str(self.root)
        if str(target) != root and not str(target).startswith(root + os.sep):
            raise VOSPathError(f"path escapes the virtual OS: {p}")
        return target

    def _refuse_procfs_write(self, p) -> None:
        """/proc is generated, not stored — writing to it is meaningless."""
        if self.procfs.owns(p):
            raise VOSFsError(f"read-only filesystem: {p}")

    def vname(self, target: Path) -> str:
        try:
            return "/" + str(Path(target).relative_to(self.root))
        except ValueError:
            return str(target)

    # ------------------------------------------------------------ stat ops
    def exists(self, p) -> bool:
        if self.procfs.owns(p):
            return self.procfs.exists(p)
        return self.vpath(p).exists()

    def is_dir(self, p) -> bool:
        if self.procfs.owns(p):
            return self.procfs.is_dir(p)
        return self.vpath(p).is_dir()

    def size(self, p) -> int:
        if self.procfs.owns(p):
            if self.procfs.is_dir(p):
                return 4096
            try:
                return len(self.procfs.read(p))
            except (KeyError, IsADirectoryError):
                return 0
        f = self.vpath(p)
        return 4096 if f.is_dir() else f.stat().st_size

    def listdir(self, p) -> list[dict]:
        if self.procfs.owns(p):
            self.vpath(p)
            try:
                return self.procfs.listdir(p)
            except KeyError as e:
                raise VOSFsError(f"not a directory: {p}") from e
        d = self.vpath(p)
        if not d.is_dir():
            raise VOSFsError(f"not a directory: {p}")
        out = []
        for entry in d.iterdir():
            st = entry.stat()
            out.append({
                "name": entry.name,
                "isdir": entry.is_dir(),
                "size": 4096 if entry.is_dir() else st.st_size,
                "mtime": st.st_mtime,
            })
        out.sort(key=lambda e: (not e["isdir"], e["name"].lower()))
        return out

    def walk_files(self, p) -> list[str]:
        d = self.vpath(p)
        if not d.exists():
            raise VOSFsError(f"no such path: {p}")
        if d.is_file():
            return [self.vname(d)]
        return [self.vname(f) for f in sorted(d.rglob("*")) if f.is_file()]

    # ------------------------------------------------------------ content
    def read(self, p) -> str:
        # /proc is generated on read, never stored.
        if self.procfs.owns(p):
            self.vpath(p)  # still enforce the sandbox on the path itself
            try:
                return self.procfs.read(p)
            except IsADirectoryError as e:
                raise VOSFsError(f"is a directory: {p}") from e
            except KeyError as e:
                raise VOSFsError(f"no such file: {p}") from e
        f = self.vpath(p)
        if f.is_dir():
            raise VOSFsError(f"is a directory: {p}")
        if not f.exists():
            raise VOSFsError(f"no such file: {p}")
        return f.read_text(encoding="utf-8", errors="replace")

    def write(self, p, content: str, create_dirs: bool = True) -> int:
        self._refuse_procfs_write(p)
        f = self.vpath(p)
        if create_dirs:
            f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))

    def append(self, p, text: str) -> None:
        self._refuse_procfs_write(p)
        f = self.vpath(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def mkdir(self, p) -> None:
        self._refuse_procfs_write(p)
        self.vpath(p).mkdir(parents=True, exist_ok=True)

    def touch(self, p) -> None:
        self._refuse_procfs_write(p)
        f = self.vpath(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()

    def remove(self, p) -> None:
        self._refuse_procfs_write(p)
        f = self.vpath(p)
        if not f.exists():
            raise VOSFsError(f"no such path: {p}")
        if f.is_dir():
            shutil.rmtree(f)
        else:
            f.unlink()

    def copy(self, src, dst) -> None:
        self._refuse_procfs_write(dst)
        s, d = self.vpath(src), self.vpath(dst)
        if d.is_dir():
            d = d / s.name
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)

    def move(self, src, dst) -> None:
        self._refuse_procfs_write(src)
        self._refuse_procfs_write(dst)
        s, d = self.vpath(src), self.vpath(dst)
        if d.is_dir():
            d = d / s.name
        d.parent.mkdir(parents=True, exist_ok=True)
        s.rename(d)

    def search(self, pattern: str, path: str = "/", limit: int = 200) -> list[tuple[str, int, str]]:
        import re
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        hits = []
        for vp in self.walk_files(path):
            try:
                for i, line in enumerate(self.read(vp).splitlines(), 1):
                    if rx.search(line):
                        hits.append((vp, i, line.strip()[:200]))
                        if len(hits) >= limit:
                            return hits
            except (OSError, VOSPathError, VOSFsError):
                continue
        return hits

    # ---------------------------------------------------------------- disk
    def disk_usage(self) -> tuple[int, int]:
        total, count = 0, 0
        for f in self.root.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
                count += 1
        return total, count

    # ------------------------------------------------------------ packages
    def _pkgfile(self) -> Path:
        return self.vpath("/var/lib/vos/packages.json")

    def installed_packages(self) -> list[str]:
        f = self._pkgfile()
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (ValueError, OSError):
            return []

    def set_installed_packages(self, names: list[str]) -> None:
        self._pkgfile().write_text(
            json.dumps(sorted(set(names)), indent=2) + "\n", encoding="utf-8"
        )

    # ----------------------------------------------------------- processes
    def add_process(self, name: str, cmdline: str, service: str | None = None) -> int:
        pid = self.next_pid
        self.next_pid += 1
        self.processes[pid] = {
            "pid": pid, "name": name, "cmdline": cmdline,
            "started": time.time(), "service": service,
        }
        if service:
            self.services[service] = {"pid": pid, "state": "running"}
        return pid

    def remove_process(self, pid: int) -> None:
        proc = self.processes.pop(pid, None)
        if proc and proc.get("service"):
            self.services.pop(proc["service"], None)

    def stop_service(self, name: str) -> bool:
        svc = self.services.get(name)
        if not svc:
            return False
        self.remove_process(svc["pid"])
        return True

    def service_state(self, name: str) -> str | None:
        svc = self.services.get(name)
        return svc["state"] if svc else None

    def uptime_str(self) -> str:
        secs = int(time.time() - self.boot_time)
        if secs < 60:
            return f"up {secs} seconds"
        if secs < 3600:
            m = secs // 60
            return f"up {m} minute{'s' if m != 1 else ''}"
        return f"up {secs // 3600} hours, {(secs % 3600) // 60} minutes"
