"""A synthetic /proc filesystem.

Every file here is generated at the moment it is read, from live kernel
state — nothing is stored on disk. That is what makes the illusion hold up:
`cat /proc/uptime` twice gives two different answers, and a process that was
just killed disappears from /proc immediately.

Supported::

    /proc/uptime      seconds since boot, idle time
    /proc/meminfo     memory totals
    /proc/cpuinfo     the virtual CPU
    /proc/loadavg     load averages + running/total processes
    /proc/version     kernel version string
    /proc/mounts      mounted filesystems
    /proc/filesystems supported filesystem types
    /proc/stat        boot time, context switches
    /proc/self        -> the agent's own pid directory
    /proc/<pid>/cmdline, /comm, /status, /stat

The overlay is read-only. Writes and deletes under /proc are refused by the
kernel, exactly as they would be for most of a real procfs.
"""
from __future__ import annotations

import time

# Virtual machine specs, kept consistent with `free` and `df`.
MEM_TOTAL_KB = 2 * 1024 * 1024      # 2 GiB
SWAP_TOTAL_KB = 512 * 1024

STATIC_FILES = ("uptime", "meminfo", "cpuinfo", "loadavg", "version",
                "mounts", "filesystems", "stat")


def _fmt_kv(pairs, width=16) -> str:
    return "".join(f"{k + ':':<{width}}{v}\n" for k, v in pairs)


class ProcFS:
    """Generates /proc content on demand from VOS state."""

    def __init__(self, vos):
        self.vos = vos

    # ------------------------------------------------------------- routing
    @staticmethod
    def owns(path: str) -> bool:
        """True if this path lives under /proc."""
        p = "/" + str(path).strip("/")
        return p == "/proc" or p.startswith("/proc/")

    def _parts(self, path: str) -> list[str]:
        p = "/" + str(path).strip("/")
        rest = p[len("/proc"):].strip("/")
        return [seg for seg in rest.split("/") if seg]

    def _pids(self) -> list[int]:
        return sorted(self.vos.processes)

    def _agent_pid(self) -> int:
        for pid, proc in sorted(self.vos.processes.items()):
            if "agent" in proc.get("name", ""):
                return pid
        return self._pids()[0] if self._pids() else 1

    # --------------------------------------------------------------- query
    def exists(self, path: str) -> bool:
        parts = self._parts(path)
        if not parts:
            return True
        head = parts[0]
        if head == "self":
            head = str(self._agent_pid())
        if len(parts) == 1:
            return head in STATIC_FILES or (head.isdigit() and int(head) in self.vos.processes)
        if len(parts) == 2 and head.isdigit():
            return (int(head) in self.vos.processes
                    and parts[1] in ("cmdline", "comm", "status", "stat"))
        return False

    def is_dir(self, path: str) -> bool:
        parts = self._parts(path)
        if not parts:
            return True
        head = parts[0]
        if head == "self":
            head = str(self._agent_pid())
        return len(parts) == 1 and head.isdigit() and int(head) in self.vos.processes

    def listdir(self, path: str) -> list[dict]:
        parts = self._parts(path)
        now = time.time()

        def entry(name, isdir=False, size=0):
            return {"name": name, "isdir": isdir, "size": size, "mtime": now}

        if not parts:
            out = [entry(name, size=len(self.read(f"/proc/{name}")))
                   for name in STATIC_FILES]
            out.append(entry("self", isdir=True, size=4096))
            out += [entry(str(pid), isdir=True, size=4096) for pid in self._pids()]
            return out

        head = parts[0]
        if head == "self":
            head = str(self._agent_pid())
        if len(parts) == 1 and head.isdigit():
            return [entry(n, size=len(self.read(f"/proc/{head}/{n}")))
                    for n in ("cmdline", "comm", "stat", "status")]
        raise KeyError(path)

    # ---------------------------------------------------------------- read
    def read(self, path: str) -> str:
        parts = self._parts(path)
        if not parts:
            raise IsADirectoryError("/proc")

        head = parts[0]
        if head == "self":
            head = str(self._agent_pid())

        if len(parts) == 1 and head in STATIC_FILES:
            return getattr(self, f"_read_{head}")()

        if head.isdigit():
            pid = int(head)
            proc = self.vos.processes.get(pid)
            if proc is None:
                raise KeyError(path)
            if len(parts) == 1:
                raise IsADirectoryError(path)
            return self._read_pid(pid, proc, parts[1])

        raise KeyError(path)

    # --------------------------------------------------------- static files
    def _uptime(self) -> float:
        return max(0.0, time.time() - self.vos.boot_time)

    def _read_uptime(self) -> str:
        up = self._uptime()
        # Idle time counts once per virtual core.
        return f"{up:.2f} {up * 1.87:.2f}\n"

    def _read_meminfo(self) -> str:
        used_bytes, _ = self.vos.disk_usage()
        # A plausible resident footprint that grows a little with real usage.
        used_kb = 96 * 1024 + used_bytes // 1024
        free_kb = max(1024, MEM_TOTAL_KB - used_kb)
        return _fmt_kv([
            ("MemTotal", f"{MEM_TOTAL_KB:>12} kB"),
            ("MemFree", f"{free_kb:>12} kB"),
            ("MemAvailable", f"{int(free_kb * 0.92):>12} kB"),
            ("Buffers", f"{16 * 1024:>12} kB"),
            ("Cached", f"{64 * 1024:>12} kB"),
            ("SwapTotal", f"{SWAP_TOTAL_KB:>12} kB"),
            ("SwapFree", f"{SWAP_TOTAL_KB:>12} kB"),
            ("Processes", f"{len(self.vos.processes):>12}"),
        ])

    def _read_cpuinfo(self) -> str:
        blocks = []
        for i in range(2):
            blocks.append(_fmt_kv([
                ("processor", i),
                ("model name", "MServerOS Virtual CPU @ 1.00GHz"),
                ("vendor_id", "MServerOS"),
                ("cpu family", 1),
                ("cpu MHz", "1000.000"),
                ("cache size", "512 KB"),
                ("flags", "vos sandbox nofpu"),
            ], width=14))
        return "\n".join(blocks)

    def _read_loadavg(self) -> str:
        running = 1
        total = len(self.vos.processes)
        last = max(self.vos.processes) if self.vos.processes else 0
        return f"0.08 0.03 0.01 {running}/{total} {last}\n"

    def _read_version(self) -> str:
        from .kernel import OS_NAME, OS_VERSION
        return (f"{OS_NAME} version {OS_VERSION} (vos@mserver) "
                f"(python virtual kernel) #1 SMP\n")

    def _read_mounts(self) -> str:
        return ("vrootfs / vos rw,relatime 0 0\n"
                "proc /proc proc rw,nosuid,nodev,noexec 0 0\n"
                "tmpfs /tmp tmpfs rw,nosuid,nodev 0 0\n")

    def _read_filesystems(self) -> str:
        return "nodev\tproc\nnodev\ttmpfs\n\tvos\n"

    def _read_stat(self) -> str:
        up = self._uptime()
        return (f"cpu  {int(up * 10)} 0 {int(up * 3)} {int(up * 87)} 0 0 0 0 0 0\n"
                f"btime {int(self.vos.boot_time)}\n"
                f"processes {len(self.vos.processes)}\n"
                f"procs_running 1\n")

    # ----------------------------------------------------------- per-process
    def _read_pid(self, pid: int, proc: dict, leaf: str) -> str:
        name = proc.get("name", "?")
        cmdline = proc.get("cmdline", name)
        if leaf == "cmdline":
            # Real procfs uses NUL separators; newline is friendlier in a
            # shell that has no hexdump.
            return cmdline + "\n"
        if leaf == "comm":
            return name + "\n"
        if leaf == "stat":
            started = proc.get("started", self.vos.boot_time)
            ticks = int(max(0.0, started - self.vos.boot_time) * 100)
            return f"{pid} ({name}) S 1 {pid} {pid} 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {ticks}\n"
        if leaf == "status":
            svc = proc.get("service")
            age = int(max(0.0, time.time() - proc.get("started", time.time())))
            return _fmt_kv([
                ("Name", name),
                ("State", "S (sleeping)"),
                ("Pid", pid),
                ("PPid", 1 if pid != 1 else 0),
                ("Service", svc or "-"),
                ("RunningFor", f"{age}s"),
                ("Cmdline", cmdline),
                ("Threads", 1),
            ])
        raise KeyError(leaf)
