"""Tool risk tiers and the confirmation gate.

Every tool the agent can call is currently trusted equally: ``vos_read`` and
``vos_delete`` sit side by side with the same authority. The standard advice
for agents that can act is to separate read-only work from reversible writes
from irreversible destruction, and to require deterministic authorisation —
not model self-restraint — before the last of those runs.

Three tiers:

``READ``
    Cannot change anything. Always allowed.
``WRITE``
    Changes state but is recoverable from a snapshot. Always allowed.
``DESTRUCTIVE``
    Deletes or overwrites in a way that loses data. Gated.

The gate is deliberately *deterministic* — a plain Python check on the tool
name and its arguments. It does not ask the model whether the call is safe,
because the model is exactly the component that might be confused.

Modes:

``ask``    (default in the interactive REPL) — prompt the user, y/N
``allow``  — permit everything, for scripted or headless runs (``--yolo``)
``deny``   — refuse every destructive call, for an untrusted session
"""
from __future__ import annotations

import re

READ = "read"
WRITE = "write"
DESTRUCTIVE = "destructive"

TIERS = {
    "vos_read": READ,
    "vos_list": READ,
    "vos_search": READ,
    "pkg_list": READ,
    "services": READ,
    "present": READ,
    "snapshot_list": READ,
    "vos_write": WRITE,
    "vos_edit": WRITE,
    "pkg_install": WRITE,
    "dashboard": WRITE,
    "snapshot_save": WRITE,
    "vos_run": WRITE,          # refined below by inspecting the command
    "pkg_remove": WRITE,
    "vos_delete": DESTRUCTIVE,
    "snapshot_rollback": DESTRUCTIVE,
}

# Shell commands that destroy data. `vos_run` is normally a WRITE, but it is
# the tool through which everything else can happen, so its argument is
# inspected rather than trusted.
_DESTRUCTIVE_CMD = re.compile(
    r"""(?:^|[|;&]\s*)\s*
        (?:rm\s+(?:-\w+\s+)*|              # rm, any flags
           mv\s+.*\s+/(?:\s|$)|            # mv over the root
           snapshot\s+(?:rm|remove|delete|rollback|restore)\b
        )""",
    re.VERBOSE,
)

# `rm` of a path that is clearly a whole subtree of the OS. The target must be
# "/" itself, "/*", or a top-level system directory — NOT merely any absolute
# path, or `rm /tmp/scratch.txt` would look like a catastrophe.
_SYSTEM_DIRS = "etc|root|usr|var|bin|sbin|srv|home|opt|lib"
_ROOT_WIPE = re.compile(
    r"""\brm\s+(?:-\w+\s+)*        # rm with any flags
        (?:
            /\*?\s*$               # "/" or "/*" as the final argument
          | /(?:""" + _SYSTEM_DIRS + r""")/?\*?\s*$   # a whole system dir
        )""",
    re.VERBOSE,
)


def is_destructive_command(cmd: str) -> bool:
    """Does this shell command line destroy data?

    Exported so the scheduler can refuse to run destructive work unattended:
    a cron job fires with nobody watching, so the confirmation gate can never
    prompt, and scheduling would otherwise be a way around it.
    """
    return bool(_DESTRUCTIVE_CMD.search(cmd or ""))


def classify(tool: str, args: dict | None = None) -> str:
    """Return the risk tier for a tool call."""
    args = args or {}
    tier = TIERS.get(tool, WRITE)
    if tool == "vos_run":
        cmd = str(args.get("command", ""))
        if _DESTRUCTIVE_CMD.search(cmd):
            return DESTRUCTIVE
    return tier


def describe(tool: str, args: dict | None = None) -> str:
    """One-line human description of what is about to happen."""
    args = args or {}
    if tool == "vos_run":
        return f"run: {args.get('command', '')}"
    if tool == "vos_delete":
        return f"delete: {args.get('path', '')}"
    if tool == "snapshot_rollback":
        return f"roll the whole filesystem back to: {args.get('name', '')}"
    bits = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
    return f"{tool}({bits})"


def is_root_wipe(tool: str, args: dict | None = None) -> bool:
    """True for calls that would erase a large part of the vOS."""
    args = args or {}
    if tool == "vos_run":
        return bool(_ROOT_WIPE.search(str(args.get("command", ""))))
    if tool == "vos_delete":
        p = str(args.get("path", "")).rstrip("/")
        return p in ("", "/", "/etc", "/root", "/usr", "/var", "/bin", "/sbin", "/srv")
    return False


class Gate:
    """Deterministic authorisation for destructive tool calls.

    ``confirm`` is a callable ``(prompt: str) -> bool``. It is injected so the
    REPL can ask on the terminal, the web chat can refuse, and tests can drive
    it directly without any I/O.
    """

    def __init__(self, mode: str = "ask", confirm=None, on_snapshot=None):
        self.mode = mode if mode in ("ask", "allow", "deny") else "ask"
        self._confirm = confirm
        self._on_snapshot = on_snapshot
        self.blocked = 0
        self.approved = 0

    def check(self, tool: str, args: dict | None = None) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a pending call."""
        tier = classify(tool, args)
        if tier != DESTRUCTIVE:
            return True, ""

        if self.mode == "allow":
            self._auto_snapshot(tool, args)
            self.approved += 1
            return True, ""

        if self.mode == "deny":
            self.blocked += 1
            return False, (
                "refused: this session runs with destructive actions disabled. "
                f"Not performed: {describe(tool, args)}")

        # mode == "ask"
        if self._confirm is None:
            # No way to ask (headless web chat): fail closed.
            self.blocked += 1
            return False, (
                "refused: destructive actions need confirmation, and this "
                "session cannot prompt. Re-run it from the terminal, or start "
                f"mserver with --yolo. Not performed: {describe(tool, args)}")

        warn = "  ⚠ THIS WOULD ERASE A LARGE PART OF THE vOS\n" if is_root_wipe(tool, args) else ""
        prompt = (f"\n{warn}  The agent wants to: {describe(tool, args)}\n"
                  f"  Allow? [y/N] ")
        try:
            ok = bool(self._confirm(prompt))
        except (EOFError, KeyboardInterrupt):
            ok = False
        if ok:
            self._auto_snapshot(tool, args)
            self.approved += 1
            return True, ""
        self.blocked += 1
        return False, (f"refused by the user. Not performed: "
                       f"{describe(tool, args)}")

    def _auto_snapshot(self, tool: str, args: dict | None) -> None:
        """Take an automatic snapshot before a large destructive action."""
        if self._on_snapshot and is_root_wipe(tool, args):
            try:
                self._on_snapshot(f"before {describe(tool, args)[:60]}")
            except Exception:
                pass
