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

from . import network as netmod
from . import packages as pkgmod
from . import snapshots as snapmod
from . import userpkg
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


def tokenize(line: str, expand=None) -> list[str]:
    """Minimal shell tokenizer: spaces, quotes, \\, pipes, >, >>, ;, && and ||.

    ``expand`` is an optional ``fn(text) -> text`` applied to unquoted and
    double-quoted fragments. Single-quoted fragments are passed through
    verbatim, which is what makes ``echo '$HOME'`` print the literal text.
    """
    def ex(text: str) -> str:
        return expand(text) if expand else text

    tokens: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in " \t":
            i += 1
            continue
        if line.startswith("&&", i):
            tokens.append("&&")
            i += 2
            continue
        if line.startswith("||", i):
            tokens.append("||")
            i += 2
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
                if quote == '"' and line[i] == "\\" and i + 1 < n and line[i + 1] in "\"\\$":
                    buf.append(line[i + 1])
                    i += 2
                    continue
                buf.append(line[i])
                i += 1
            i += 1
            text = "".join(buf)
            # Single quotes are literal; double quotes still expand.
            tokens.append(ex(text) if quote == '"' else text)
            continue
        if c == "\\" and i + 1 < n:
            tokens.append(line[i + 1])
            i += 2
            continue
        buf = []
        while i < n and line[i] not in " \t|>\"'\\;&":
            buf.append(line[i])
            i += 1
        tokens.append(ex("".join(buf)))
    return tokens


# Positional parameters ($1..$9, $@, $#) let agent-authored package
# commands take arguments; they are set by userpkg._make_runner.
_VAR_RE = re.compile(r"\$(\{)?([A-Za-z_][A-Za-z0-9_]*|[0-9]|\?|\$|@|#)(?(1)\})")

# Extra usage detail for `man`, beyond the one-line help string.
MAN_EXTRA = {
    "ls": "ls [-l] [-a] [path]\n-l  long listing\n-a  include dotfiles",
    "grep": "grep [-n] pattern [file]\n-n  show line numbers\nReads stdin when no file is given.",
    "snapshot": ("snapshot list\nsnapshot save <name> [label]\n"
                 "snapshot rollback <name>\nsnapshot rm <name>\n\n"
                 "Snapshots are stored outside the vOS, so 'rm -rf /' cannot\n"
                 "destroy them. A rollback saves the current state first."),
    "pkg": "pkg list\npkg search <term>\npkg info <name>\npkg install <name>\npkg remove <name>",
    "service": "service\nservice <name> start|stop|status",
    "export": "export NAME=value\nexport            (list the environment)",
    "alias": "alias                 list aliases\nalias ll='ls -la'     define one",
    "echo": "echo [-n] text\n$VAR and ${VAR} are expanded; single quotes are literal.",
    "find": "find [path] [-name glob]",
    "history": "history\nPersisted to /root/.msh_history across restarts.",
}

HISTORY_PATH = "/root/.msh_history"
MSHRC_PATH = "/root/.mshrc"
MAX_HISTORY = 500


class Shell:
    def __init__(self, vos: VOS):
        self.vos = vos
        self.cwd = "/"
        self.history: list[str] = []
        self.recent: list[tuple[float, str, str]] = []  # (ts, cmd, output head)
        self._plugin_cmds: dict[str, object] = {}
        self._plugin_owner: dict[str, str] = {}
        self._help: dict[str, str] = {}
        self.snapshots = snapmod.SnapshotStore(vos)
        self.aliases: dict[str, str] = {}
        self.last_status = 0
        self.env: dict[str, str] = {
            "HOME": "/root", "USER": "root", "LOGNAME": "root",
            "OS": f"{OS_NAME} {OS_VERSION}", "SHELL": f"/bin/{SHELL_NAME}",
            "TERM": "termux", "LANG": "en_US.UTF-8", "MSERVER": "1",
            "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
        }

        registry = pkgmod.full_registry(vos)
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
            "ping": S.cmd_ping, "ifconfig": S.cmd_ifconfig,
            "ip": S.cmd_ip, "netstat": S.cmd_netstat,
            "curl": S.cmd_curl, "wget": S.cmd_wget,
            "host": S.cmd_host, "nslookup": S.cmd_nslookup,
            "snapshot": S.cmd_snapshot,
            "export": S.cmd_export, "unset": S.cmd_unset,
            "alias": S.cmd_alias, "unalias": S.cmd_unalias,
            "source": S.cmd_source, "man": S.cmd_man,
            "dmesg": S.cmd_dmesg, "logger": S.cmd_logger,
            "sort": S.cmd_sort, "uniq": S.cmd_uniq, "cut": S.cmd_cut,
            "tr": S.cmd_tr, "rev": S.cmd_rev, "tee": S.cmd_tee,
            "seq": S.cmd_seq, "true": S.cmd_true, "false": S.cmd_false,
            "yes": S.cmd_yes, "basename": S.cmd_basename,
            "dirname": S.cmd_dirname, "stat": S.cmd_stat, "du": S.cmd_du,
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
            "ping": "ping a host on the virtual network (ping [-c n] host)",
            "ifconfig": "show network interfaces",
            "ip": "show addresses or routes (ip addr | ip route)",
            "netstat": "show listening ports (netstat -tln)",
            "curl": "fetch a URL from the vOS network (curl [-i|-I] url)",
            "wget": "download a URL into a file (wget [-O file] url)",
            "host": "resolve a hostname to an address",
            "nslookup": "query the virtual DNS resolver",
            "snapshot": "snapshots (snapshot [save|rollback|rm|list] [name])",
            "export": "set an environment variable (export NAME=value)",
            "unset": "remove an environment variable (unset NAME)",
            "alias": "define or list aliases (alias ll='ls -la')",
            "unalias": "remove an alias (unalias ll)",
            "source": "run commands from a file (source /root/.mshrc)",
            "man": "manual page for a command (man ls)",
            "sort": "sort lines (sort [-r] [-n] [-u] [file])",
            "uniq": "collapse repeated adjacent lines (uniq [-c])",
            "cut": "select fields (cut -d: -f1 [file])",
            "tr": "translate or delete characters (tr a-z A-Z | tr -d x)",
            "rev": "reverse each line",
            "tee": "write stdin to a file and pass it on (tee [-a] file)",
            "seq": "print a number sequence (seq [first [incr]] last)",
            "true": "do nothing, successfully",
            "false": "do nothing, unsuccessfully",
            "yes": "repeat a string",
            "basename": "strip directory from a path",
            "dirname": "strip the last component from a path",
            "stat": "file details (size, type, mtime)",
            "du": "disk usage of a path (du [-h] [path])",
            "dmesg": "kernel ring buffer messages",
            "logger": "write a message to /var/log/syslog",
        })
        self._load_history()
        self._run_rc()

    # --------------------------------------------------------- registration
    @property
    def commands(self) -> dict:
        return {**self._handlers, **self._plugin_cmds}

    def command_names(self) -> set:
        """Every name that currently resolves as a command."""
        return set(self._handlers) | set(self._plugin_cmds) | set(self.aliases)

    def register_command(self, name, fn, help_text, owner) -> None:
        self._plugin_cmds[name] = fn
        self._help[name] = help_text
        self._plugin_owner[name] = owner

    def unregister_command(self, name, owner) -> None:
        if self._plugin_owner.get(name) == owner:
            self._plugin_cmds.pop(name, None)
            self._plugin_owner.pop(name, None)
            self._help.pop(name, None)

    # ------------------------------------------------------------ environment
    def expand(self, text: str) -> str:
        """Substitute $VAR, ${VAR}, $? and $$ in a fragment.

        An undefined variable expands to the empty string, as a real shell
        does. ``$?`` is the previous command's exit status.
        """
        if "$" not in text:
            return text

        def sub(m):
            name = m.group(2)
            if name == "?":
                return str(self.last_status)
            if name == "$":
                return "1"  # msh runs as a single "process"
            if name == "PWD":
                return self.cwd
            return self.env.get(name, "")

        return _VAR_RE.sub(sub, text)

    def _resolve_alias(self, line: str, depth: int = 0) -> str:
        """Expand a leading alias, guarding against alias loops."""
        if depth > 10:
            return line
        stripped = line.lstrip()
        if not stripped:
            return line
        head, _, rest = stripped.partition(" ")
        target = self.aliases.get(head)
        if target is None:
            return line
        expanded = f"{target} {rest}".strip() if rest else target
        # An alias whose body starts with its own name (ll='ls -la') must not
        # recurse forever.
        if expanded.split(" ")[0] == head:
            return expanded
        return self._resolve_alias(expanded, depth + 1)

    def _run_rc(self) -> None:
        """Execute /root/.mshrc at startup.

        The file is created at boot with `alias ll='ls -la'` in it; before
        this it was written and never read.
        """
        try:
            if not self.vos.exists(MSHRC_PATH):
                return
            body = self.vos.read(MSHRC_PATH)
        except Exception:
            return
        for raw in body.splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                try:
                    self.run(line, _record=False)
                except Exception:
                    pass

    def _load_history(self) -> None:
        try:
            if self.vos.exists(HISTORY_PATH):
                self.history = self.vos.read(HISTORY_PATH).splitlines()[-MAX_HISTORY:]
        except Exception:
            self.history = []

    def save_history(self) -> None:
        """Persist history so it survives a restart."""
        try:
            self.vos.write(HISTORY_PATH,
                           "\n".join(self.history[-MAX_HISTORY:]) + "\n")
        except Exception:
            pass

    # -------------------------------------------------------------- running
    def run(self, line: str, stdin: str | None = None,
            _record: bool = True) -> tuple[str, str, int]:
        line = line.strip()
        if not line:
            return "", "", 0
        if _record:
            self.history.append(line)
            if len(self.history) > MAX_HISTORY:
                self.history = self.history[-MAX_HISTORY:]
        line = self._resolve_alias(line)
        out, err, code = "", "", 0
        try:
            # (joiner, tokens) — joiner says how this segment relates to the
            # previous one: "|" pipe, ";" sequence, "&&" on success, "||" on
            # failure.
            segs: list[tuple[str, list[str]]] = [("", [])]
            for t in tokenize(line, expand=self.expand):
                if t in ("|", ";", "&&", "||"):
                    segs.append((t, []))
                else:
                    segs[-1][1].append(t)
            cur_stdin = stdin
            for idx, (joiner, toks) in enumerate(segs):
                if joiner == "&&" and code != 0:
                    break
                if joiner == "||" and code == 0:
                    break
                out, err, code = self._segment(toks, cur_stdin)
                self.last_status = code
                # Feed stdout onward only when the NEXT segment is a pipe.
                nxt = segs[idx + 1][0] if idx + 1 < len(segs) else ""
                cur_stdin = out if nxt == "|" else None
        except (VOSPathError, VOSFsError) as e:
            out, err, code = "", str(e), 1
        except Exception as e:  # keep the vOS alive no matter what
            out, err, code = "", f"msh: internal error: {e}", 1
        self.last_status = code
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

    # ------------------------------------------------------------ network
    def cmd_ping(self, args, stdin):
        count = 4
        rest = []
        i = 0
        while i < len(args):
            if args[i] == "-c" and i + 1 < len(args):
                try:
                    count = int(args[i + 1])
                except ValueError:
                    return "", "ping: bad count", 1
                i += 2
                continue
            rest.append(args[i])
            i += 1
        if not rest:
            return "", "usage: ping [-c count] <host>", 1
        out, code = self.vos.network.ping(rest[0], count)
        return (out, "", 0) if code == 0 else ("", out, code)

    def cmd_ifconfig(self, args, stdin):
        ifs = self.vos.network.interfaces()
        want = args[0] if args else None
        if want and want not in ifs:
            return "", f"ifconfig: interface {want} does not exist", 1
        blocks = []
        for name in ([want] if want else ifs):
            d = ifs[name]
            head = f"{name}: flags=4163<{d['flags']}>  mtu {d['mtu']}"
            body = [f"        inet {d['ip']}  netmask {d['netmask']}"]
            if d.get("broadcast"):
                body[-1] += f"  broadcast {d['broadcast']}"
            if d.get("mac"):
                body.append(f"        ether {d['mac']}  txqueuelen 1000  (Ethernet)")
            else:
                body.append("        loop  txqueuelen 1000  (Local Loopback)")
            body.append("        RX packets 0  bytes 0 (0.0 B)")
            body.append("        TX packets 0  bytes 0 (0.0 B)")
            blocks.append(head + "\n" + "\n".join(body))
        return "\n\n".join(blocks), "", 0

    def cmd_ip(self, args, stdin):
        net = self.vos.network
        sub = args[0] if args else "addr"
        if sub in ("a", "addr", "address", "link"):
            ifs = net.interfaces()
            out = []
            for i, (name, d) in enumerate(ifs.items(), start=1):
                out.append(f"{i}: {name}: <{d['flags']}> mtu {d['mtu']}")
                if d.get("mac"):
                    out.append(f"    link/ether {d['mac']}")
                else:
                    out.append("    link/loopback 00:00:00:00:00:00")
                if sub != "link":
                    out.append(f"    inet {d['ip']} scope global {name}")
            return "\n".join(out), "", 0
        if sub in ("r", "route"):
            return (f"default via {netmod.GATEWAY} dev eth0\n"
                    f"10.0.2.0/24 dev eth0 proto kernel scope link src "
                    f"{netmod.HOST_IP}"), "", 0
        return "", f"ip: unknown object {sub!r} (try: addr, link, route)", 1

    def cmd_netstat(self, args, stdin):
        lis = self.vos.network.listeners()
        head = ("Active Internet connections (only servers)\n"
                "Proto Recv-Q Send-Q Local Address           "
                "Foreign Address         State       PID/Program name")
        rows = [
            f"{l['proto']:<6}{0:>6} {0:>6} {l['addr'] + ':' + str(l['port']):<24}"
            f"{'0.0.0.0:*':<24}{l['state']:<12}{l['pid']}/{l['service']}"
            for l in lis
        ]
        return head + ("\n" + "\n".join(rows) if rows else ""), "", 0

    def cmd_host(self, args, stdin):
        if not args:
            return "", "usage: host <name>", 1
        name = args[0]
        ip = self.vos.network.resolve(name)
        if ip is None:
            return "", f"Host {name} not found: 3(NXDOMAIN)", 1
        return f"{name} has address {ip}", "", 0

    def cmd_nslookup(self, args, stdin):
        if not args:
            return "", "usage: nslookup <name>", 1
        name = args[0]
        ip = self.vos.network.resolve(name)
        head = f"Server:\t\t{netmod.DNS}\nAddress:\t{netmod.DNS}#53\n"
        if ip is None:
            return "", head + f"\n** server can't find {name}: NXDOMAIN", 1
        return head + f"\nName:\t{name}\nAddress: {ip}", "", 0

    def _http(self, url):
        net = self.vos.network
        host, port, path = netmod.parse_url(url)
        return net.http_get(host, port, path), host, port, path

    def cmd_curl(self, args, stdin):
        show_headers = False
        head_only = False
        silent = False
        url = None
        for a in args:
            if a in ("-i", "--include"):
                show_headers = True
            elif a in ("-I", "--head"):
                head_only = True
            elif a in ("-s", "--silent"):
                silent = True
            elif a.startswith("-"):
                continue
            else:
                url = a
        if not url:
            return "", "usage: curl [-i|-I|-s] <url>", 2
        try:
            resp, host, port, path = self._http(url)
        except netmod.NetworkError as e:
            return "", f"curl: (7) {e}", 7
        hdr = (f"HTTP/1.1 {resp['status']} {resp['reason']}\n" +
               "\n".join(f"{k}: {v}" for k, v in resp["headers"].items()))
        self.vos.syslog.service(
            "nginx", f'"GET {path} HTTP/1.1" {resp["status"]} '
                     f'{resp["headers"]["Content-Length"]}')
        if head_only:
            return hdr, "", 0
        if show_headers:
            return hdr + "\n\n" + resp["body"], "", 0
        if resp["status"] >= 400 and not silent:
            return resp["body"], "", 22
        return resp["body"], "", 0

    def cmd_wget(self, args, stdin):
        url = None
        outfile = None
        i = 0
        while i < len(args):
            if args[i] in ("-O", "-o") and i + 1 < len(args):
                outfile = args[i + 1]
                i += 2
                continue
            if not args[i].startswith("-"):
                url = args[i]
            i += 1
        if not url:
            return "", "usage: wget [-O file] <url>", 1
        try:
            resp, host, port, path = self._http(url)
        except netmod.NetworkError as e:
            return "", f"wget: unable to resolve or connect: {e}", 4
        if resp["status"] >= 400:
            return "", f"wget: server returned error: HTTP/1.1 {resp['status']} {resp['reason']}", 8
        name = outfile or (path.rstrip("/").rsplit("/", 1)[-1] or "index.html")
        dest = name if name.startswith("/") else self._join(self.cwd, name)
        self.vos.write(dest, resp["body"])
        size = resp["headers"]["Content-Length"]
        self.vos.syslog.service("nginx", f'"GET {path} HTTP/1.1" {resp["status"]} {size}')
        return (f"Connecting to {host}:{port}... connected.\n"
                f"HTTP request sent, awaiting response... {resp['status']} {resp['reason']}\n"
                f"Length: {size}\nSaving to: '{self.vos.vname(self.vos.vpath(dest))}'\n\n"
                f"'{name}' saved [{size}]"), "", 0

    def cmd_dmesg(self, args, stdin):
        text = self.vos.syslog.dmesg(self.vos.boot_time)
        return (text + "\n" if text else "(kernel ring buffer is empty)"), "", 0

    def cmd_logger(self, args, stdin):
        message = " ".join(args) if args else (stdin or "").strip()
        if not message:
            return "", "logger: nothing to log", 1
        self.vos.syslog.write("user", message)
        return "", "", 0

    # ------------------------------------------------------------ coreutils
    def _text_in(self, args, stdin) -> str:
        """Read from the named files, or from stdin when none are given."""
        files = [a for a in args if not a.startswith("-")]
        if files:
            return "".join(self.vos.read(f) for f in files)
        return stdin or ""

    def cmd_sort(self, args, stdin):
        text = self._text_in(args, stdin)
        lines = text.splitlines()
        flags = {a for a in args if a.startswith("-")}
        if "-n" in flags:
            def key(s):
                m = re.match(r"\s*(-?\d+)", s)
                return (0, int(m.group(1))) if m else (1, 0)
            lines.sort(key=key)
        else:
            lines.sort()
        if "-r" in flags:
            lines.reverse()
        if "-u" in flags:
            seen, out = set(), []
            for ln in lines:
                if ln not in seen:
                    seen.add(ln)
                    out.append(ln)
            lines = out
        return ("\n".join(lines) + "\n" if lines else ""), "", 0

    def cmd_uniq(self, args, stdin):
        text = self._text_in(args, stdin)
        count = "-c" in args
        out, prev, n = [], None, 0
        for ln in text.splitlines():
            if ln == prev:
                n += 1
                continue
            if prev is not None:
                out.append(f"{n:>7} {prev}" if count else prev)
            prev, n = ln, 1
        if prev is not None:
            out.append(f"{n:>7} {prev}" if count else prev)
        return ("\n".join(out) + "\n" if out else ""), "", 0

    def cmd_cut(self, args, stdin):
        delim, fields = "\t", None
        rest = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-d" and i + 1 < len(args):
                delim = args[i + 1] or "\t"
                i += 2
            elif a.startswith("-d"):
                delim = a[2:] or "\t"
                i += 1
            elif a == "-f" and i + 1 < len(args):
                fields = args[i + 1]
                i += 2
            elif a.startswith("-f"):
                fields = a[2:]
                i += 1
            else:
                rest.append(a)
                i += 1
        if not fields:
            return "", "cut: you must specify a list of fields (-f)", 1
        try:
            wanted = []
            for part in fields.split(","):
                if "-" in part.strip("-") and not part.startswith("-"):
                    a, _, b = part.partition("-")
                    wanted.extend(range(int(a), int(b) + 1))
                else:
                    wanted.append(int(part))
        except ValueError:
            return "", f"cut: invalid field list: {fields}", 1
        text = self._text_in(rest, stdin)
        out = []
        for ln in text.splitlines():
            cols = ln.split(delim)
            out.append(delim.join(cols[i - 1] for i in wanted
                                  if 0 < i <= len(cols)))
        return ("\n".join(out) + "\n" if out else ""), "", 0

    @staticmethod
    def _tr_set(spec: str) -> str:
        """Expand a tr character set: 'a-z0-9_' -> the literal characters."""
        out, i = [], 0
        while i < len(spec):
            if i + 2 < len(spec) and spec[i + 1] == "-" and spec[i + 2] >= spec[i]:
                out.extend(chr(c) for c in range(ord(spec[i]), ord(spec[i + 2]) + 1))
                i += 3
            else:
                out.append(spec[i])
                i += 1
        return "".join(out)

    def cmd_tr(self, args, stdin):
        rest = [a for a in args if not a.startswith("-")]
        if "-d" in args and rest:
            drop = self._tr_set(rest[0])
            return (stdin or "").translate({ord(c): None for c in drop}), "", 0
        if len(rest) < 2:
            return "", "tr: usage: tr SET1 SET2  (or tr -d SET)", 1
        src = self._tr_set(rest[0])
        dst = self._tr_set(rest[1])
        if not dst:
            return "", "tr: empty replacement set", 1
        if len(dst) < len(src):          # pad, as tr does
            dst = dst + dst[-1] * (len(src) - len(dst))
        return (stdin or "").translate(str.maketrans(src, dst[:len(src)])), "", 0

    def cmd_rev(self, args, stdin):
        text = self._text_in(args, stdin)
        return "\n".join(ln[::-1] for ln in text.splitlines()) + "\n", "", 0

    def cmd_tee(self, args, stdin):
        text = stdin or ""
        for f in [a for a in args if not a.startswith("-")]:
            if "-a" in args:
                self.vos.append(f, text)
            else:
                self.vos.write(f, text, create_dirs=True)
        return text, "", 0

    def cmd_seq(self, args, stdin):
        nums = [a for a in args if not a.startswith("-")]
        try:
            vals = [int(x) for x in nums]
        except ValueError:
            return "", "seq: arguments must be integers", 1
        if len(vals) == 1:
            rng = range(1, vals[0] + 1)
        elif len(vals) == 2:
            rng = range(vals[0], vals[1] + 1)
        elif len(vals) == 3:
            rng = range(vals[0], vals[2] + 1, vals[1])
        else:
            return "", "seq: usage: seq [first [incr]] last", 1
        return "\n".join(str(i) for i in rng) + "\n", "", 0

    def cmd_true(self, args, stdin):
        return "", "", 0

    def cmd_false(self, args, stdin):
        return "", "", 1

    def cmd_yes(self, args, stdin):
        word = " ".join(args) if args else "y"
        return "\n".join([word] * 10) + "\n", "", 0

    def cmd_basename(self, args, stdin):
        if not args:
            return "", "basename: needs a path", 1
        name = args[0].rstrip("/").rsplit("/", 1)[-1] or "/"
        if len(args) > 1 and name.endswith(args[1]) and name != args[1]:
            name = name[: -len(args[1])]
        return name + "\n", "", 0

    def cmd_dirname(self, args, stdin):
        if not args:
            return "", "dirname: needs a path", 1
        p = args[0].rstrip("/")
        head = p.rsplit("/", 1)[0] if "/" in p else "."
        return (head or "/") + "\n", "", 0

    def cmd_stat(self, args, stdin):
        if not args:
            return "", "stat: needs a path", 1
        out = []
        for a in args:
            p = self.vos.vpath(a)
            if not p.exists():
                return "", f"stat: no such file: {a}", 1
            st = p.stat()
            kind = "directory" if p.is_dir() else "regular file"
            out.append(
                f"  File: {self.vos.vname(p)}\n"
                f"  Size: {st.st_size:<10} Type: {kind}\n"
                f"Modify: {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}"
            )
        return "\n".join(out), "", 0

    def cmd_du(self, args, stdin):
        target = next((a for a in args if not a.startswith("-")), self.cwd)
        p = self.vos.vpath(target)
        if not p.exists():
            return "", f"du: no such path: {target}", 1
        total = 0
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        else:
            total = p.stat().st_size
        if "-h" in args:
            return f"{snapmod.fmt_size(total)}\t{target}\n", "", 0
        return f"{max(1, total // 1024)}\t{target}\n", "", 0

    def cmd_env(self, args, stdin):
        items = dict(self.env)
        items["PWD"] = self.cwd
        return "\n".join(f"{k}={v}" for k, v in sorted(items.items())), "", 0

    def cmd_export(self, args, stdin):
        if not args:
            return self.cmd_env(args, stdin)
        for a in args:
            if "=" not in a:
                # `export NAME` with no value: keep whatever is set.
                self.env.setdefault(a, "")
                continue
            name, _, value = a.partition("=")
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                return "", f"export: invalid variable name: {name}", 1
            self.env[name] = value
        return "", "", 0

    def cmd_unset(self, args, stdin):
        for a in args:
            self.env.pop(a, None)
        return "", "", 0

    def cmd_alias(self, args, stdin):
        if not args:
            if not self.aliases:
                return "(no aliases)", "", 0
            return "\n".join(f"alias {k}='{v}'"
                             for k, v in sorted(self.aliases.items())), "", 0
        joined = " ".join(args)
        if "=" not in joined:
            out = []
            for a in args:
                if a in self.aliases:
                    out.append(f"alias {a}='{self.aliases[a]}'")
                else:
                    out.append(f"alias: {a}: not found")
            return "\n".join(out), "", 0
        name, _, value = joined.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", name):
            return "", f"alias: invalid name: {name}", 1
        self.aliases[name] = value
        return "", "", 0

    def cmd_unalias(self, args, stdin):
        for a in args:
            self.aliases.pop(a, None)
        return "", "", 0

    def cmd_source(self, args, stdin):
        if not args:
            return "", "source: needs a file", 1
        try:
            body = self.vos.read(args[0])
        except (VOSPathError, VOSFsError) as e:
            return "", f"source: {e}", 1
        outs = []
        for raw in body.splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                o, e, _ = self.run(line, _record=False)
                if o:
                    outs.append(o)
                if e:
                    outs.append(e)
        return "\n".join(outs), "", 0

    def cmd_man(self, args, stdin):
        """Manual pages, built from the help text the shell already carries."""
        if not args:
            return "", "man: what manual page do you want? (try: man ls)", 1
        name = args[0]
        if name not in self.commands:
            return "", f"man: no manual entry for {name}", 1
        desc = self._help.get(name, "(no description)")
        owner = self._plugin_owner.get(name)
        origin = f"provided by package '{owner}'" if owner else "msh builtin"
        lines = [
            f"{name.upper()}(1)                     MServerOS Manual                     {name.upper()}(1)",
            "",
            "NAME",
            f"    {name} — {desc}",
            "",
            "DESCRIPTION",
            f"    {origin}.",
        ]
        extra = MAN_EXTRA.get(name)
        if extra:
            lines += ["", "USAGE"] + [f"    {ln}" for ln in extra.splitlines()]
        lines += ["", "SEE ALSO", "    help — list every available command", ""]
        return "\n".join(lines), "", 0

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
            return ("usage: pkg list | pkg search <term> | pkg info <name> | "
                    "pkg install <name> | pkg remove <name> | pkg created | "
                    "pkg source <name> | pkg delete <name>"), "", 1
        sub = args[0]
        registry = pkgmod.full_registry(self.vos)
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
                self.vos.syslog.write("vos-pkg", f"installed {name} {p.version}")
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
                self.vos.syslog.write("vos-pkg", f"removed {name}")
                out.append(f"Removed {name}")
            return "\n".join(out), "", 0
        if sub in ("created", "mine"):
            pkgs = userpkg.UserPkgStore(self.vos).all()
            if not pkgs:
                return "(no agent-created packages yet)", "", 0
            installed = set(self.vos.installed_packages())
            rows = ["  NAME          VERSION   COMMANDS"]
            for n in sorted(pkgs):
                p = pkgs[n]
                mark = "*" if n in installed else " "
                cmds = ", ".join(sorted(p.commands)) or "-"
                rows.append(f"  {mark:<1}{n:<13}{p.version:<10}{cmds}")
            return "\n".join(rows) + "\n  (* installed)", "", 0
        if sub == "source":
            if len(args) < 2:
                return "pkg: source requires a package name", "", 1
            p = userpkg.UserPkgStore(self.vos).get(args[1])
            if not p:
                return f"pkg: no agent-created package: {args[1]}", "", 1
            out = [f"# {p.name} {p.version} - {p.description}"]
            for fpath, content in sorted(p.files.items()):
                out.append(f"\n--- file: {fpath} ---\n{content}")
            for cname, spec in sorted(p.commands.items()):
                out.append(f"\n--- command: {cname} ---\n# {spec.get('help','')}")
                out.extend(spec.get("body", []))
            return "\n".join(out), "", 0
        if sub == "delete":
            if len(args) < 2:
                return "pkg: delete requires a package name", "", 1
            name = args[1]
            store = userpkg.UserPkgStore(self.vos)
            if not store.exists(name):
                return f"pkg: no agent-created package: {name}", "", 1
            if name in self.vos.installed_packages():
                self.run(f"pkg remove {name}", _record=False)
            try:
                store.remove(name)
            except userpkg.UserPkgError as e:
                return f"pkg: {e}", "", 1
            self.vos.syslog.write("vos-pkg", f"deleted user package {name}")
            return f"Deleted agent-created package {name}", "", 0
        return f"pkg: unknown subcommand: {sub}", "", 1

    def cmd_snapshot(self, args, stdin):
        """Filesystem snapshots — the undo button for the whole vOS."""
        store = self.snapshots
        action = args[0] if args else "list"

        if action in ("list", "ls"):
            snaps = store.list()
            if not snaps:
                return ("(no snapshots yet — 'snapshot save <name>' takes one)",
                        "", 0)
            width = max([len(s["name"]) for s in snaps] + [16])
            lines = [f"  {'NAME':<{width}}  {'AGE':<9} {'FILES':>5}  {'SIZE':<9} LABEL"]
            for s in snaps:
                lines.append(
                    f"  {s['name']:<{width}}  {snapmod.fmt_age(s['created']):<9} "
                    f"{s['files']:>5}  {snapmod.fmt_size(s['bytes']):<9} {s.get('label', '')}"
                )
            lines.append(f"  ({len(snaps)} snapshot(s), "
                         f"{snapmod.fmt_size(store.total_bytes())} total)")
            return "\n".join(lines), "", 0

        if action in ("save", "take", "create"):
            name = args[1] if len(args) > 1 else None
            label = " ".join(args[2:]) if len(args) > 2 else ""
            try:
                meta = store.save(name, label=label)
            except snapmod.SnapshotError as e:
                return "", f"snapshot: {e}", 1
            return (f"saved snapshot '{meta['name']}' "
                    f"({meta['files']} files, {snapmod.fmt_size(meta['bytes'])})"), "", 0

        if action in ("rollback", "restore"):
            if len(args) < 2:
                return "", "snapshot: rollback needs a name (see 'snapshot list')", 1
            try:
                meta = store.rollback(args[1])
            except snapmod.SnapshotError as e:
                return "", f"snapshot: {e}", 1
            # The rootfs was swapped underneath us; cwd may no longer exist.
            if not self.vos.is_dir(self.cwd):
                self.cwd = "/"
            return (f"rolled back to '{meta['name']}'. "
                    f"Previous state saved as '{meta['undo']}' — "
                    f"'snapshot rollback {meta['undo']}' to undo this."), "", 0

        if action in ("rm", "remove", "delete"):
            if len(args) < 2:
                return "", "snapshot: rm needs a name", 1
            try:
                store.remove(args[1])
            except snapmod.SnapshotError as e:
                return "", f"snapshot: {e}", 1
            return f"removed snapshot '{args[1]}'", "", 0

        return "", (f"snapshot: unknown action '{action}' "
                    f"(use: list, save, rollback, rm)"), 1

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
            # Remember it so a reboot brings it back up.
            self.vos.set_enabled_service(name, {
                "binary": SERVICE_DEFS[name]["binary"],
                "cmdline": SERVICE_DEFS[name]["cmdline"],
            })
            self.vos.syslog.service(name, f"started (pid {pid})")
            return f"{name} started (pid {pid})", "", 0
        if action == "stop":
            self.vos.set_enabled_service(name, None)
            if self.vos.stop_service(name):
                try:
                    self.vos.remove(f"/var/run/{name}.pid")
                except (VOSPathError, VOSFsError):
                    pass
                self.vos.syslog.service(name, "stopped")
                return f"{name} stopped", "", 0
            return f"service {name} is not running", "", 0
        state = self.vos.service_state(name)
        if state:
            return f"{name} is running (pid {self.vos.services[name]['pid']})", "", 0
        return f"{name} is stopped", "", 0
