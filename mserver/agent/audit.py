"""Tool-call audit log.

Every action the agent takes is appended to ``/var/log/agent.log`` *inside the
vOS*, so the record lives in the same sandbox as the thing it describes and is
readable with the tools the user already has::

    !tail -n 20 /var/log/agent.log
    !grep vos_delete /var/log/agent.log

Logging every tool execution is the standard recommendation for an agent with
filesystem access: it is what makes a bad run reconstructable afterwards. It
also fills ``/var/log``, which the vOS creates at boot and otherwise never
writes to.

Failures to log are swallowed. An audit log that can break the agent loop is
worse than no audit log.
"""
from __future__ import annotations

import time

LOG_PATH = "/var/log/agent.log"
MAX_FIELD = 160
MAX_LINES = 2000


def _clip(value, limit: int = MAX_FIELD) -> str:
    s = str(value).replace("\n", "\\n").replace("\r", "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _fmt_args(args: dict) -> str:
    if not args:
        return "-"
    return " ".join(f"{k}={_clip(v, 60)}" for k, v in args.items())


class AuditLog:
    """Appends one line per tool call to the vOS log."""

    def __init__(self, vos, path: str = LOG_PATH):
        self.vos = vos
        self.path = path

    def log(self, tool: str, args: dict, result: str = "", ok: bool = True,
            source: str = "agent") -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        status = "ok" if ok else "ERR"
        line = (f"{ts} [{source}] {status} {tool} "
                f"args({_fmt_args(args)}) -> {_clip(result)}\n")
        try:
            self._append(line)
        except Exception:
            # Never let auditing break the agent.
            pass

    def note(self, text: str, source: str = "system") -> None:
        """Log a non-tool event (compaction, loop guard, session start)."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._append(f"{ts} [{source}] -- {_clip(text, 400)}\n")
        except Exception:
            pass

    def _append(self, line: str) -> None:
        p = self.vos.vpath(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 512_000:
            self._rotate(p)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _rotate(self, p) -> None:
        """Keep the log bounded — a phone has finite storage."""
        try:
            lines = p.read_text("utf-8", "replace").splitlines(keepends=True)
            p.write_text("".join(lines[-MAX_LINES // 2:]), encoding="utf-8")
        except OSError:
            pass


class LoopGuard:
    """Detects an agent repeating the same call.

    A doom loop is the same (tool, args) fingerprint recurring inside a
    sliding window of recent calls. Without this an agent can burn every one
    of its steps re-running ``ls`` and report nothing useful.
    """

    def __init__(self, window: int = 20, threshold: int = 3):
        self.window = window
        self.threshold = threshold
        self._calls: list[str] = []

    def check(self, tool: str, args: dict) -> str | None:
        """Record a call; return a warning string if it looks like a loop."""
        fp = f"{tool}:{sorted((str(k), str(v)) for k, v in (args or {}).items())}"
        self._calls.append(fp)
        if len(self._calls) > self.window:
            self._calls = self._calls[-self.window:]
        count = self._calls.count(fp)
        if count >= self.threshold:
            return (f"NOTE: you have called {tool} with these exact arguments "
                    f"{count} times. It will not return anything new. Change "
                    f"the arguments, try a different tool, or finish the task "
                    f"and reply to the user.")
        return None

    def reset(self) -> None:
        self._calls.clear()
