"""msh — the MServerOS shell.

A tiny but real command interpreter: globbing, pipes, > / >> redirection,
a current directory, history, and pluggable commands installed by packages.
"""
from __future__ import annotations

import fnmatch
import random
import re
import time
from datetime import datetime

from . import packages as pkgmod
from .kernel import (
    OS_NAME,
    OS_VERSION,
    SHELL_NAME,
    SHELL_VERSION,
    VOS,
    VOSFsError,
    VOSPathError,
)

SERVICE_DEFS = {
    "ssh": {"binary": "sshd", "cmdline": "sshd: /usr/sbin/sshd -D"},
    "nginx": {"binary": "nginx", "cmdline": "nginx: master process /usr/sbin/nginx"},
}

# Commands whose arguments must NOT be glob-expanded (patterns, etc).
NO_GLOB = {
    "cd", "echo", "grep", "kill", "pkg", "service", "which", "help",
    "history", "clear", "reboot", "env", "date", "whoami", "hostname",
    "uname", "uptime", "ps", "free", "df", "neofetch", "wc",
}


def tokenize(line: str) -> list[str]:
    """Minimal shell tokenizer: spaces, quotes, \\, pipes, >, >> and ;."""
    tokens: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in " \t":
            i += 1
            continue
        if line.startswith(">>", i):
            tokens.append(">>")
            i += 2
            continue
        if c == ">":
            tokens.append(">")
            i += 1
            continue
        if c == "|":
            tokens.append("|")
            i += 1
            continue
        if c == ";":
            tokens.append(";")
            i += 1
            continue
        if c in "\"'":
            quote, i, buf = c, i + 1, []
            while i < n and line[i] != quote:
                if quote == '"' and line[i] == "\\" and i + 1 < n and line[i + 1] in "\"\\":
                    buf.append(line[i + 1])
                    i += 2
                    continue
                buf.append(line[i])
                i += 1
            i += 1
            tokens.append("".join(buf))
            continue
        if c == "\\" and i + 1 < n:
            tokens.append(line[i + 1])
            i += 2
            continue
        buf = []
        while i < n and line[i] not in " \t|>\"'\\;":
            buf.append(line[i])
            i += 1
        tokens.append("".join(buf))
    return tokens


class Shell:
    def __init__(self, vos: VOS):
        self.vos = vos
        self.cwd = "/"
        self.history: list[str] = []
        self.recent: list[tuple[float, str, str]] = []  # (ts, cmd, output head)
        self._plugin_cmds: dict[str, object] = {}
        self._plugin_owner: dict[str, str] = {}
        self._help: dict[str, str] = {}

        registry = pkgmod.build_registry()
        for name in vos.installed_packages():
            pkg = registry.get(name)
            if pkg:
                pkg.register(self)

        S = Shell
        self._handlers = {
            "help": S.cmd_help, "ls": S.cmd_ls, "cd": S.cmd_cd,
            "pwd": S.cmd_pwd, "echo": S.cmd_echo, "cat": S.cmd_cat,
            "mkdir": S.cmd_mkdir, "touch": S.cmd_touch, "rm": S.cmd_rm,
            "cp": S.cmd_cp, "mv": S.cmd_mv, "grep": S.cmd_grep,
            "head": S.cmd_head, "tail": S.cmd_tail, "wc": S.cmd_wc,
            "find": S.cmd_find, "whoami": S.cmd_whoami,
            "hostname": S.cmd_hostname, "uname": S.cmd_uname,
            "date": S.cmd_date, "uptime": S.cmd_uptime, "ps": S.cmd_ps,
            "kill": S.cmd_kill, "reboot": S.cmd_reboot,
            "clear": S.cmd_clear, "env": S.cmd_env,
            "neofetch": S.cmd_neofetch, "history": S.cmd_history,
            "which": S.cmd_which, "free": S.cmd_free, "df": S.cmd_df,
            "pkg": S.cmd_pkg, "service": S.cmd_service,
        }
        self._help.update({
            "help": "show help (help <cmd> for details)",
            "ls": "list files (ls [-la] [path])",
            "cd": "change directory (cd [path])",
            "pwd": "print working directory",
            "echo": "print text (echo [-n] ...)",
            "cat": "print files (or stdin)",
            "mkdir": "make directories",
            "touch": "create empty file / update time",
            "rm": "remove files/directories",
            "cp": "copy (cp [-r] src dst)",
            "mv": "move/rename (mv src dst)",
            "grep": "search lines (grep [-n] pattern [file])",
            "head": "first lines (head [-n N] [file])",
            "tail": "last lines (tail [-n N] [file])",
            "wc": "count lines/words/chars",
            "find": "find files (find [path] [-name glob])",
            "whoami": "current user (root)",
            "hostname": "print hostname",
            "uname": "system info (uname [-a])",
            "date": "current date/time",
            "uptime": "system uptime",
            "ps": "list processes",
            "kill": "kill a process (kill PID)",
            "reboot": "reboot the vOS",
            "clear": "clear the terminal",
            "env": "show environment",
            "neofetch": "system info with logo",
            "history": "command history",
            "which": "locate a command",
            "free": "memory usage",
            "df": "disk usage",
            "pkg": "package manager (pkg list|search|info|install|remove)",
            "service": "services (service [name] start|stop|status)",
        })

    # --------------------------------------------------------- registration
    @property
    def commands(self) -> dict:
        return {**self._handlers, **self._plugin_cmds}

    def register_command(self, name, fn, help_text, owner) -> None:
        self._plugin_cmds[name] = fn
        self._help[name] = help_text
        self._plugin_owner[name] = owner

    def unregister_command(self, name, owner) -> None:
        if self._plugin_owner.get(name) == owner:
            self._plugin_cmds.pop(name, None)
            self._plugin_owner.pop(name, None)
            self._help.pop(name, None)

    # -------------------------------------------------------------- running
    def run(self, line: str, stdin: str | None = None) -> tuple[str, str, int]:
        line = line.strip()
        if not line:
            return "", "", 0
        self.history.append(line)
        if len(self.history) > 500:
            self.history = self.history[-500:]
        out, err, code = "", "", 0
        try:
            segs: list[tuple[bool, list[str]]] = [(True, [])]
            for t in tokenize(line):
                if t == "|":
                    segs.append((True, []))
                elif t == ";":
                    segs.append((False, []))
                else:
                    segs[-1][1].append(t)
            cur_stdin = stdin
            for piped, toks in segs:
                out, err, code = self._segment(toks, cur_stdin)
                cur_stdin = out if piped else None
        except (VOSPathError, VOSFsError) as e:
            out, err, code = "", str(e), 1
        except Exception as e:  # keep the vOS alive no matter what
            out, err, code = "", f"msh: internal error: {e}", 1
        head = (out or err or "").splitlines()[:2]
        self.recent.append((time.time(), line, " ".join(head)[:120]))
        if len(self.recent) > 200:
            self.recent = self.recent[-200:]
        return out, err, code

    def _segment(self, toks: list[str], stdin) -> tuple[str, str, int]:
        if not toks:
            return "", "", 0
        redir, append = None, False
        i = 0
        while i < len(toks):
            if toks[i] in (">", ">>"):
                if i + 1 >= len(toks):
                    raise VOSFsError("missing target for redirection")
                redir, append = toks[i + 1], toks[i] == ">>"
                toks = toks[:i] + toks[i + 2:]
            else:
                i += 1
        name = toks[0]
        args = toks[1:]
        fn = self.commands.get(name)
        if fn is None:
            return "", f"msh: {name}: command not found (try: pkg search {name})", 127
        if name not in NO_GLOB:
            args = self._expand(args)
        try:
            out, err, code = fn(self, args, stdin)
        except (VOSPathError, VOSFsError) as e:
            return "", f"{name}: {e}", 1
        except Exception as e:
            return "", f"{name}: internal error: {e}", 1
        if redir is not None:
            if append:
                self.vos.append(redir, out)
            else:
                self.vos.write(redir, out, create_dirs=True)
            return "", err, code
        return out, err, code

    def _expand(self, args: list[str]) -> list[str]:
        out = []
        for a in args:
            if a.startswith("-") or not any(ch in a for ch in "*?["):
                out.append(a)
                continue
            base, pat = (a.rsplit("/", 1) + ["/"])[:2] if "/" in a else (self.cwd, a)
            base = base or "/"
            if not self.vos.is_dir(base):
                out.append(a)
                continue
            matches = [
                self._join(base, e["name"])
                for e in self.vos.listdir(base)
                if fnmatch.fnmatch(e["name"], pat)
            ]
            out.extend(matches or [a])
        return out

    def _join(self, cwd: str, p: str) -> str:
        parts = (cwd.rstrip("/") + "/" + str(p)).split("/")
        stack = []
        for x in parts:
            if x in ("", "."):
                continue
            if x == "..":
                if stack:
                    stack.pop()
                continue
            stack.append(x)
        return "/" + "/".join(stack)

    def _flags(self, args: list[str], letters: str) -> tuple[set, list]:
        flags, rest = set(), []
        for a in args:
            if a.startswith("-") and len(a) > 1 and all(c in letters for c in a[1:]):
                flags.update(a[1:])
            else:
                rest.append(a)
        return flags, rest

    def _count(self, args: list[str], default: int) -> tuple[int, list]:
        n, rest, i = default, [], 0
        while i < len(args):
            if args[i] == "-n" and i + 1 < len(args):
                try:
                    n = max(0, int(args[i + 1]))
                except ValueError:
                    pass
                i += 2
                continue
            rest.append(args[i])
            i += 1
        return n, rest

    # -------------------------------------------------------------- commands
    def cmd_help(self, args, stdin):
        if args:
            name = args[0]
            if name in self._help:
                return self._help[name], "", 0
            return f"no help for {name}", "", 1
        names = sorted(set(self._handlers) | set(self._plugin_cmds))
        rows = [f"  {n:<10} {self._help.get(n, '')}" for n in names]
        tail = [
            "",
            "  * pipe with |  ·  redirect with > / >>  ·  glob with * ? [",
            "  * software:    pkg list | pkg search <term> | pkg install <name>",
            "  * daemons:     service [name] start|stop|status",
        ]
        return "\n".join(["MServerOS (msh) — available commands:"] + rows + tail), "", 0

    def cmd_ls(self, args, stdin):
        flags, paths = self._flags(args, "la")
        blocks = []
        for path in (paths or [self.cwd]):
            if not self.vos.is_dir(path):
                if self.vos.exists(path):
                    blocks.append(str(path))
                    continue
                return "", f"ls: no such file or directory: {path}", 1
            entries = self.vos.listdir(path)
            if "a" not in flags:
                entries = [e for e in entries if not e["name"].startswith(".")]
            if "l" not in flags:
                lines = [e["name"] + ("/" if e["isdir"] else "") for e in entries]
                blocks.append("\n".join(lines))
                continue
            lines = []
            for e in entries:
                perm = "drwxr-xr-x" if e["isdir"] else "-rw-r--r--"
                date = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M")
                lines.append(f"{perm}  root  {e['size']:>7}  {date}  {e['name']}")
            blocks.append("\n".join(lines) if lines else "(empty)")
        return "\n\n".join(blocks), "", 0

    def cmd_cd(self, args, stdin):
        target = args[0] if args else "/root"
        t = self._join(self.cwd, target if target.startswith("/") else target)
        if not self.vos.is_dir(t):
            return "", f"cd: no such directory: {t}", 1
        self.cwd = t
        return "", "", 0

    def cmd_pwd(self, args, stdin):
        return self.cwd, "", 0

    def cmd_echo(self, args, stdin):
        nl = not (args and args[0] == "-n")
        text = " ".join(args[1:] if args and args[0] == "-n" else args)
        return text + ("\n" if nl else ""), "", 0

    def cmd_cat(self, args, stdin):
        if not args:
            return (stdin or ""), "", 0
        parts = [self.vos.read(f) for f in args]
        return "\n".join(parts), "", 0

    def cmd_mkdir(self, args, stdin):
        _, paths = self._flags(args, "p")
        if not paths:
            return "", "mkdir: missing operand", 1
        for p in paths:
            self.vos.mkdir(p)
        return "", "", 0

    def cmd_touch(self, args, stdin):
        if not args:
            return "", "touch: missing operand", 1
        for p in args:
            self.vos.touch(p)
        return "", "", 0

    def cmd_rm(self, args, stdin):
        _, paths = self._flags(args, "rf")
        if not paths:
            return "", "rm: missing operand", 1
        for p in paths:
            self.vos.remove(p)
        return "", "", 0

    def cmd_cp(self, args, stdin):
        _, paths = self._flags(args, "r")
        if len(paths) != 2:
            return "", "cp: usage: cp [-r] src dst", 1
        self.vos.copy(paths[0], paths[1])
        return "", "", 0

    def cmd_mv(self, args, stdin):
        _, paths = self._flags(args, "")
        if len(paths) != 2:
            return "", "mv: usage: mv src dst", 1
        self.vos.move(paths[0], paths[1])
        return "", "", 0

    def cmd_grep(self, args, stdin):
        flags = {a[1:] for a in args if a.startswith("-") and len(a) > 1}
        rest = [a for a in args if not a.startswith("-")]
        if not rest:
            return "grep: missing pattern", "", 1
        pattern = rest[0]
        text = self.vos.read(rest[1]) if len(rest) > 1 else (stdin or "")
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        out = []
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{i}:{line}" if "n" in flags else line)
        return "\n".join(out), "", (0 if out else 1)

    def cmd_head(self, args, stdin):
        n, rest = self._count(args, 10)
        text = self.vos.read(rest[0]) if rest else (stdin or "")
        return "\n".join(text.splitlines()[:n]), "", 0

    def cmd_tail(self, args, stdin):
        n, rest = self._count(args, 10)
        text = self.vos.read(rest[0]) if rest else (stdin or "")
        lines = text.splitlines()
        return "\n".join(lines[-n:] if n else []), "", 0

    def cmd_wc(self, args, stdin):
        flag_l = bool(args) and args[0] == "-l"
        srcs = args[1:] if flag_l else args
        l = w = c = 0
        if srcs:
            for f in srcs:
                t = self.vos.read(f)
                l += len(t.splitlines())
                w += len(t.split())
                c += len(t)
        else:
            t = stdin or ""
            l, w, c = len(t.splitlines()), len(t.split()), len(t)
        if flag_l:
            return str(l), "", 0
        return f"{l} {w} {c}", "", 0

    def cmd_find(self, args, stdin):
        name_pat, rest = None, []
        for i, a in enumerate(args):
            if a == "-name" and i + 1 < len(args):
                name_pat = args[i + 1]
            elif not a.startswith("-"):
                rest.append(a)
        path = rest[0] if rest else self.cwd
        if name_pat is None and len(rest) > 1:
            name_pat = rest[1]
        out = []
        for vp in self.vos.walk_files(path):
            if name_pat is None or fnmatch.fnmatch(vp, name_pat) or fnmatch.fnmatch(vp.rsplit("/", 1)[-1], name_pat):
                out.append(vp)
            if len(out) >= 500:
                break
        return "\n".join(out) if out else "(no files)", "", 0

    def cmd_whoami(self, args, stdin):
        return "root", "", 0

    def cmd_hostname(self, args, stdin):
        return self.vos.hostname, "", 0

    def cmd_uname(self, args, stdin):
        if args and args[0] in ("-a", "--all"):
            return (
                f"{OS_NAME} {self.vos.hostname} vos-{OS_VERSION} "
                f"msh-{SHELL_VERSION} python3 android (termux)",
                "", 0,
            )
        return OS_NAME, "", 0

    def cmd_date(self, args, stdin):
        return datetime.now().strftime("%a %Y-%m-%d %H:%M:%S"), "", 0

    def cmd_uptime(self, args, stdin):
        load = (
            f"{random.uniform(0.02, 0.15):.2f}, "
            f"{random.uniform(0.01, 0.10):.2f}, "
            f"{random.uniform(0.01, 0.08):.2f}"
        )
        return f" {datetime.now().strftime('%H:%M:%S')} {self.vos.uptime_str()},  load average: {load}", "", 0

    def cmd_ps(self, args, stdin):
        lines = [f"  {'PID':<6}{'TTY':<8}{'TIME':<10}CMD"]
        for pid in sorted(self.vos.processes):
            p = self.vos.processes[pid]
            el = int(time.time() - p["started"])
            tstr = f"00:0{el // 60}:{el % 60:02d}"
            lines.append(f"  {pid:<6}{'pts/0':<8}{tstr:<10}{p['cmdline']}")
        return "\n".join(lines), "", 0

    def cmd_kill(self, args, stdin):
        if not args:
            return "kill: missing operand", "", 1
        try:
            pid = int(args[0])
        except ValueError:
            return f"kill: invalid pid: {args[0]}", "", 1
        if pid not in self.vos.processes:
            return f"kill: no such process: {pid}", "", 1
        self.vos.remove_process(pid)
        return f"process {pid} terminated", "", 0

    def cmd_reboot(self, args, stdin):
        self.vos.reboot()
        self.cwd = "/"
        return f"{OS_NAME} rebooted. Fresh process table ready — run 'ps' to see it.", "", 0

    def cmd_clear(self, args, stdin):
        return "", "", 0

    def cmd_env(self, args, stdin):
        return "\n".join([
            "HOME=/root", "USER=root", "LOGNAME=root",
            f"OS={OS_NAME} {OS_VERSION}", f"SHELL=/bin/{SHELL_NAME}",
            "TERM=termux", "LANG=en_US.UTF-8",
            f"PWD={self.cwd}", "MSERVER=1",
        ]), "", 0

    def cmd_neofetch(self, args, stdin):
        logo = [
            "     ████        ",
            "   ██    ██      ",
            "   ██    ██      ",
            "   ████████      ",
            "   ██    ██      ",
            "   ████████      ",
            "     ████        ",
        ]
        used, files = self.vos.disk_usage()
        info = [
            f"root@{self.vos.hostname}",
            "─────────────────────",
            f"OS: {OS_NAME} {OS_VERSION} (virtual Linux)",
            "Kernel: vos 1.0 / python3",
            f"Shell: {SHELL_NAME} {SHELL_VERSION}",
            f"Uptime: {self.vos.uptime_str()}",
            f"Packages: {len(self.vos.installed_packages())} (vos-pkg)",
            f"Disk: {used // 1024} KiB used ({files} files)",
            "Terminal: mserver-agent",
            "Resolution: 1080x2340",
        ]
        rows = []
        for i in range(max(len(logo), len(info))):
            l = logo[i] if i < len(logo) else " " * 16
            r = info[i] if i < len(info) else ""
            rows.append(f"{l} {r}")
        return "\n".join(rows), "", 0

    def cmd_history(self, args, stdin):
        if not self.history:
            return "(empty)", "", 0
        start = max(1, len(self.history) - 19)
        return "\n".join(
            f"  {i:3d}  {h}" for i, h in enumerate(self.history[-20:], start=start)
        ), "", 0

    def cmd_which(self, args, stdin):
        if not args:
            return "which: missing operand", "", 1
        name = args[0]
        if name in self._handlers:
            return f"/bin/{name}", "", 0
        if name in self._plugin_cmds:
            return f"/usr/bin/{name}", "", 0
        return "", f"which: {name} not found", 1

    def cmd_free(self, args, stdin):
        used = random.randint(256, 900)
        total = 4096
        return (
            f"              total        used        free\n"
            f"Mem:          {total}M       {used}M       {total - used}M\n"
            f"Swap:          512M          0M         512M"
        ), "", 0

    def cmd_df(self, args, stdin):
        used, files = self.vos.disk_usage()
        return (
            "Filesystem      Size      Used      Avail  Mounted on\n"
            f"vrootfs         2.0G   {used // 1024:>6} KiB     1.9G    /"
        ), "", 0

    def cmd_pkg(self, args, stdin):
        if not args:
            return "usage: pkg list | pkg search <term> | pkg info <name> | pkg install <name> | pkg remove <name>", "", 1
        sub = args[0]
        registry = pkgmod.build_registry()
        if sub == "list":
            installed = set(self.vos.installed_packages())
            lines = [f"  {'*':<3}NAME          VERSION   DESCRIPTION"]
            for name in sorted(registry):
                p = registry[name]
                mark = "*" if name in installed else " "
                lines.append(f"  {mark:<3}{name:<12}{p.version:<11}{p.description}")
            return "\n".join(lines) + "\n  (* installed)", "", 0
        if sub == "search":
            term = (args[1] if len(args) > 1 else "").lower()
            rows = [
                f"  {n} {registry[n].version} - {registry[n].description}"
                for n in sorted(registry)
                if term in n or term in registry[n].description.lower()
            ]
            return "\n".join(rows) if rows else "(no matches)", "", 0
        if sub == "info":
            if len(args) < 2:
                return "pkg: info requires a package name", "", 1
            p = registry.get(args[1])
            if not p:
                return f"pkg: unknown package: {args[1]}", "", 1
            state = "installed" if args[1] in self.vos.installed_packages() else "not installed"
            files = "\n".join(f"  {f}" for f in p.files)
            return f"{p.name} {p.version}  [{state}]\n{p.description}\nFiles:\n{files}", "", 0
        if sub == "install":
            if len(args) < 2:
                return "pkg: install requires a package name", "", 1
            out = []
            for name in args[1:]:
                p = registry.get(name)
                if not p:
                    out.append(f"pkg: unknown package: {name}")
                    continue
                if name in self.vos.installed_packages():
                    out.append(f"{name} {p.version} is already installed")
                    continue
                for fpath, content in p.files.items():
                    self.vos.write(fpath, content)
                p.register(self)
                self.vos.set_installed_packages(self.vos.installed_packages() + [name])
                out.append(f"Installing {name} {p.version} ... OK ({len(p.files)} files)")
            return "\n".join(out), "", 0
        if sub == "remove":
            if len(args) < 2:
                return "pkg: remove requires a package name", "", 1
            out = []
            installed = self.vos.installed_packages()
            for name in args[1:]:
                p = registry.get(name)
                if not p:
                    out.append(f"pkg: unknown package: {name}")
                    continue
                if name not in installed:
                    out.append(f"{name} is not installed")
                    continue
                p.unregister(self)
                for fpath in p.files:
                    try:
                        self.vos.remove(fpath)
                    except (VOSPathError, VOSFsError):
                        pass
                self.vos.set_installed_packages([n for n in installed if n != name])
                out.append(f"Removed {name}")
            return "\n".join(out), "", 0
        return f"pkg: unknown subcommand: {sub}", "", 1

    def cmd_service(self, args, stdin):
        if not args:
            lines = []
            for name in SERVICE_DEFS:
                state = self.vos.service_state(name)
                pid = self.vos.services[name]["pid"] if state else "-"
                lines.append(f"  {name:<8} {state or 'stopped':<10} pid {pid:<5} {SERVICE_DEFS[name]['cmdline']}")
            return "\n".join(lines) if lines else "(no services)", "", 0
        name, action = args[0], (args[1] if len(args) > 1 else "status")
        if name not in SERVICE_DEFS:
            return f"service: unknown service: {name} (available: {', '.join(SERVICE_DEFS)})", "", 1
        if action == "start":
            if self.vos.service_state(name) == "running":
                return f"service {name} is already running", "", 0
            pid = self.vos.add_process(
                SERVICE_DEFS[name]["binary"], SERVICE_DEFS[name]["cmdline"], service=name
            )
            self.vos.write(f"/var/run/{name}.pid", f"{pid}\n")
            return f"{name} started (pid {pid})", "", 0
        if action == "stop":
            if self.vos.stop_service(name):
                try:
                    self.vos.remove(f"/var/run/{name}.pid")
                except (VOSPathError, VOSFsError):
                    pass
                return f"{name} stopped", "", 0
            return f"service {name} is not running", "", 0
        state = self.vos.service_state(name)
        if state:
            return f"{name} is running (pid {self.vos.services[name]['pid']})", "", 0
        return f"{name} is stopped", "", 0
