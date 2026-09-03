"""cron and at — the first thing in the vOS that acts on its own.

Until now the vOS only ever reacted: something happened because the user or
the agent typed a command. A scheduler changes that. `*/5 * * * * logger
backup ok` fires with nobody watching.

Design notes
------------
**Jobs run on their own Shell.** The interactive `Shell` carries mutable
state — `cwd`, `env`, alias table, history. A background thread calling the
*same* Shell would change the user's working directory underneath them
mid-command. So the scheduler builds its own Shell over the same VOS, with
cwd `/root` like a real cron, and never touches the interactive one.

**The tick is monotonic and catches up.** Wall-clock is wrong on a phone that
sleeps: `time.time()` can jump hours between ticks. Rather than fire a job
once per missed minute (a thundering herd after a suspend), each job records
the last minute it ran and fires at most once per due minute.

**A failing job must not kill the scheduler.** Every job runs inside a bare
except; failures go to `/var/log/cron.log` and the loop keeps going.

**Jobs are shell commands, never Python** — same reasoning as `pkg_create`.
They go through the ordinary interpreter and the ordinary sandbox.

**Destructive commands cannot be scheduled.** A job fires with nobody
watching, so the confirmation gate has no one to ask and would have to either
block forever or auto-approve. Auto-approving would make `schedule` a way
around every protection on `rm`. So destructive command lines are refused at
the point they are added *and* again before they run — the second check
matters because the crontab is a file, and something could write to it
directly without going through `add_job`.

**Output goes to the log, not the terminal.** Printing into a REPL from a
background thread corrupts the prompt. `/var/log/cron.log` is the mailbox.
"""
from __future__ import annotations

import json
import threading
import time

CRONTAB_PATH = "/var/spool/cron/crontabs/root"
ATJOBS_PATH = "/var/spool/cron/atjobs.json"
CRON_LOG = "cron"

MAX_JOBS = 64
MAX_OUTPUT = 2000
TICK_SECONDS = 5

# Shorthands real cron accepts.
ALIASES = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),
)


class CronError(Exception):
    pass


def _refuse_if_destructive(command: str) -> None:
    """Scheduled work runs unattended, so it can never be confirmed."""
    try:
        from ..agent.risk import is_destructive_command
    except Exception:                                # pragma: no cover
        return
    if is_destructive_command(command):
        raise CronError(
            "refusing to schedule a destructive command "
            f"({command!r}). Scheduled jobs run with nobody watching, so the "
            "confirmation gate cannot ask you first. Run it interactively "
            "instead.")


# --------------------------------------------------------------- parsing
def parse_field(spec: str, lo: int, hi: int, name: str) -> set:
    """Expand one crontab field into the set of values it matches."""
    values = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{name}: empty value")
        step = 1
        if "/" in part:
            part, _, stepstr = part.partition("/")
            try:
                step = int(stepstr)
            except ValueError:
                raise CronError(f"{name}: bad step {stepstr!r}") from None
            if step < 1:
                raise CronError(f"{name}: step must be >= 1")
        if part == "*":
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise CronError(f"{name}: bad range {part!r}") from None
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise CronError(f"{name}: bad value {part!r}") from None
        if start < lo or end > hi or start > end:
            raise CronError(
                f"{name}: {part!r} out of range ({lo}-{hi})")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"{name}: matches nothing")
    return values


def parse_schedule(spec: str) -> dict:
    """'*/5 * * * *' -> {minute: {...}, hour: {...}, ...}"""
    spec = (spec or "").strip()
    if spec in ALIASES:
        spec = ALIASES[spec]
    if spec.startswith("@"):
        raise CronError(
            f"unknown shorthand {spec!r} (try {', '.join(sorted(ALIASES))})")
    parts = spec.split()
    if len(parts) != 5:
        raise CronError(
            f"schedule needs 5 fields (min hour day month weekday), got "
            f"{len(parts)}: {spec!r}")
    return {
        name: parse_field(parts[i], lo, hi, name)
        for i, (name, lo, hi) in enumerate(FIELDS)
    }


def matches(sched: dict, tm) -> bool:
    """Does this schedule fire at the given struct_time?

    Real cron quirk: when both day-of-month and day-of-week are restricted,
    the job runs if *either* matches, not both.
    """
    if tm.tm_min not in sched["minute"]:
        return False
    if tm.tm_hour not in sched["hour"]:
        return False
    if tm.tm_mon not in sched["month"]:
        return False
    # Python: Monday=0..Sunday=6. Cron: Sunday=0..Saturday=6.
    dow = (tm.tm_wday + 1) % 7
    dom_restricted = len(sched["day"]) < 31
    dow_restricted = len(sched["weekday"]) < 7
    dom_ok = tm.tm_mday in sched["day"]
    dow_ok = dow in sched["weekday"]
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def describe(spec: str) -> str:
    """Plain-English gloss, so `crontab -l` is readable."""
    spec = ALIASES.get(spec.strip(), spec.strip())
    common = {
        "* * * * *": "every minute",
        "0 * * * *": "hourly, on the hour",
        "0 0 * * *": "daily at midnight",
        "0 0 * * 0": "weekly on Sunday",
        "0 0 1 * *": "monthly on the 1st",
        "0 0 1 1 *": "yearly on 1 January",
    }
    if spec in common:
        return common[spec]
    parts = spec.split()
    if len(parts) == 5 and parts[0].startswith("*/") and parts[1:] == ["*"] * 4:
        return f"every {parts[0][2:]} minutes"
    if len(parts) == 5 and parts[0].isdigit() and parts[1].startswith("*/"):
        return f"every {parts[1][2:]} hours at :{int(parts[0]):02d}"
    return spec


def parse_at_time(spec: str, now: float | None = None) -> float:
    """'+5m', '17:30', 'now' -> absolute epoch seconds."""
    now = time.time() if now is None else now
    spec = (spec or "").strip().lower()
    if not spec:
        raise CronError("no time given")
    if spec == "now":
        return now
    if spec.startswith("+"):
        body = spec[1:].strip()
        unit = body[-1:]
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
        if mult is None:
            num, mult = body, 60  # bare '+5' means minutes, as at(1) does
        else:
            num = body[:-1]
        try:
            return now + float(num.strip()) * mult
        except ValueError:
            raise CronError(f"bad relative time {spec!r} (try +5m)") from None
    if ":" in spec:
        try:
            hh, _, mm = spec.partition(":")
            hh, mm = int(hh), int(mm)
        except ValueError:
            raise CronError(f"bad time {spec!r} (try 17:30)") from None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise CronError(f"time out of range: {spec!r}")
        tm = time.localtime(now)
        target = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, hh, mm, 0,
                              0, 0, -1))
        if target <= now:
            target += 86400  # next occurrence, like at(1)
        return target
    raise CronError(f"cannot understand time {spec!r} (try +5m, 17:30, now)")


# ---------------------------------------------------------------- storage
class Scheduler:
    """Owns the crontab, the at queue, and the background tick thread."""

    def __init__(self, vos):
        self.vos = vos
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._shell = None
        self.runs = 0            # jobs executed, for tests and `ps`
        self._last_minute = {}   # job id -> minute stamp it last fired

    # -------------------------------------------------------------- crontab
    def _read_crontab(self) -> list:
        try:
            text = self.vos.read(CRONTAB_PATH)
        except Exception:
            return []
        jobs = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            spec = " ".join(parts[:5])
            try:
                sched = parse_schedule(spec)
            except CronError:
                continue
            # Ids number the jobs the user can see, not lines in the file —
            # `crontab -r 1` should remove the job shown as 1.
            jobs.append({"id": len(jobs) + 1, "spec": spec, "sched": sched,
                         "command": parts[5]})
        return jobs

    def list_jobs(self) -> list:
        return self._read_crontab()

    def _write_lines(self, lines: list) -> None:
        header = ("# vOS crontab — min hour day month weekday  command\n"
                  "# edit with: crontab -a '<schedule>' '<command>'\n")
        self.vos.write(CRONTAB_PATH, header + "".join(
            l if l.endswith("\n") else l + "\n" for l in lines))

    def add_job(self, spec: str, command: str) -> dict:
        parse_schedule(spec)          # validate before writing
        command = (command or "").strip()
        if not command:
            raise CronError("no command given")
        _refuse_if_destructive(command)
        with self._lock:
            jobs = self._read_crontab()
            if len(jobs) >= MAX_JOBS:
                raise CronError(f"too many cron jobs (limit {MAX_JOBS})")
            spec = ALIASES.get(spec.strip(), spec.strip())
            existing = [f"{j['spec']} {j['command']}" for j in jobs]
            existing.append(f"{spec} {command}")
            self._write_lines(existing)
        self.vos.syslog.service(CRON_LOG, f"added job: {spec} {command}")
        return {"spec": spec, "command": command}

    def remove_job(self, index: int) -> dict:
        with self._lock:
            jobs = self._read_crontab()
            match = [j for j in jobs if j["id"] == index]
            if not match:
                raise CronError(f"no cron job with id {index}")
            keep = [f"{j['spec']} {j['command']}" for j in jobs
                    if j["id"] != index]
            self._write_lines(keep)
        self.vos.syslog.service(CRON_LOG, f"removed job {index}")
        return match[0]

    def clear_jobs(self) -> int:
        with self._lock:
            n = len(self._read_crontab())
            self._write_lines([])
        self.vos.syslog.service(CRON_LOG, f"crontab cleared ({n} jobs)")
        return n

    # ------------------------------------------------------------ at queue
    def _read_at(self) -> list:
        try:
            data = json.loads(self.vos.read(ATJOBS_PATH))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write_at(self, jobs: list) -> None:
        self.vos.write(ATJOBS_PATH, json.dumps(jobs, indent=2) + "\n")

    def at_jobs(self) -> list:
        return sorted(self._read_at(), key=lambda j: j.get("when", 0))

    def add_at(self, timespec: str, command: str) -> dict:
        when = parse_at_time(timespec)
        command = (command or "").strip()
        if not command:
            raise CronError("no command given")
        _refuse_if_destructive(command)
        with self._lock:
            jobs = self._read_at()
            if len(jobs) >= MAX_JOBS:
                raise CronError(f"too many at jobs (limit {MAX_JOBS})")
            jid = max([j.get("id", 0) for j in jobs] or [0]) + 1
            job = {"id": jid, "when": when, "command": command}
            jobs.append(job)
            self._write_at(jobs)
        self.vos.syslog.write(
            CRON_LOG,
            f"queued at job {jid} for {time.strftime('%H:%M', time.localtime(when))}: {command}")
        return job

    def remove_at(self, jid: int) -> dict:
        with self._lock:
            jobs = self._read_at()
            match = [j for j in jobs if j.get("id") == jid]
            if not match:
                raise CronError(f"no at job with id {jid}")
            self._write_at([j for j in jobs if j.get("id") != jid])
        self.vos.syslog.service(CRON_LOG, f"cancelled at job {jid}")
        return match[0]

    # -------------------------------------------------------------- running
    def _job_shell(self):
        """A Shell of our own, so a job cannot move the user's cwd."""
        if self._shell is None:
            from .shell import Shell
            self._shell = Shell(self.vos)
            self._shell.cwd = "/root"
        return self._shell

    def run_command(self, command: str, source: str) -> tuple:
        """Execute one job. Never raises.

        The destructive check runs again here, not just at add time: the
        crontab is an ordinary file and something may have written to it
        without going through add_job().
        """
        try:
            _refuse_if_destructive(command)
        except CronError as e:
            self.vos.syslog.service(CRON_LOG, f"({source}) REFUSED {command}: {e}")
            return "", str(e), 1
        try:
            sh = self._job_shell()
            sh.cwd = "/root"
            out, err, code = sh.run(command, _record=False)
        except Exception as e:                      # noqa: BLE001
            self.vos.syslog.service(CRON_LOG, f"({source}) ERROR {command}: {e}")
            return "", str(e), 1
        self.runs += 1
        text = (out or "").strip()
        errtext = (err or "").strip()
        msg = f"({source}) CMD {command}"
        if code != 0:
            msg += f" [exit {code}]"
        self.vos.syslog.service(CRON_LOG, msg)
        for label, body in (("out", text), ("err", errtext)):
            if body:
                clipped = body[:MAX_OUTPUT]
                if len(body) > MAX_OUTPUT:
                    clipped += " ...[truncated]"
                for line in clipped.splitlines():
                    self.vos.syslog.service(CRON_LOG, f"  {label}: {line}")
        return out, err, code

    def tick(self, now: float | None = None) -> int:
        """Run everything due. Returns how many jobs fired.

        Called by the background thread, and directly by tests so the
        scheduler is testable without sleeping.
        """
        now = time.time() if now is None else now
        fired = 0
        tm = time.localtime(now)
        stamp = (tm.tm_year, tm.tm_yday, tm.tm_hour, tm.tm_min)

        for job in self.list_jobs():
            key = f"cron:{job['spec']}:{job['command']}"
            if self._last_minute.get(key) == stamp:
                continue          # already ran this minute
            if matches(job["sched"], tm):
                self._last_minute[key] = stamp
                self.run_command(job["command"], "cron")
                fired += 1

        due = [j for j in self._read_at() if j.get("when", 0) <= now]
        if due:
            with self._lock:
                remaining = [j for j in self._read_at()
                             if j.get("when", 0) > now]
                self._write_at(remaining)
            for job in due:
                self.run_command(job["command"], f"at:{job.get('id')}")
                fired += 1
        return fired

    # --------------------------------------------------------------- thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="vos-crond", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        self._thread = None

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:                       # noqa: BLE001
                pass          # a broken tick must never kill the daemon
            self._stop.wait(TICK_SECONDS)
