"""The vOS package registry: installable software for MServerOS.

A package is a set of files dropped into the virtual filesystem, plus
optional shell commands (plugins) registered while it stays installed.
Packages are persisted in /var/lib/vos/packages.json inside the vOS.
"""
from __future__ import annotations

import random


class Package:
    def __init__(self, name, version, description, files, commands=None):
        self.name = name
        self.version = version
        self.description = description
        self.files = files
        self.commands = commands or {}

    def register(self, shell) -> None:
        for cname, (help_text, fn) in self.commands.items():
            shell.register_command(cname, fn, help_text, self.name)

    def unregister(self, shell) -> None:
        for cname in self.commands:
            shell.unregister_command(cname, self.name)


# ----------------------------------------------------------------- plugins
def _cmd_hello(shell, args, stdin):
    return "Hello from MServerOS!", "", 0


def _cmd_cowsay(shell, args, stdin):
    phrase = " ".join(args).strip() or "Moo."
    if len(phrase) > 45:
        phrase = phrase[:44] + "…"
    box = ["_" * (len(phrase) + 2), f"< {phrase} >", "-" * (len(phrase) + 2)]
    cow = [
        "        \\   ^__^",
        "         \\  (oo)\\_______",
        "            (__)\\       )\\/\\",
        "                ||----w |",
        "                ||     ||",
    ]
    return "\n".join(box + cow), "", 0


FIGLET_FONT: dict[str, list[str]] = {
    "A": ["  ████  ", " █    █ ", " █    █ ", " ██████ ", " █    █ "],
    "B": [" █████  ", " █   █  ", " █████  ", " █   █  ", " █████  "],
    "C": [" █████  ", " █      ", " █      ", " █      ", " █████  "],
    "D": [" █████  ", " █   █  ", " █   █  ", " █   █  ", " █████  "],
    "E": [" ██████ ", " █      ", " █████  ", " █      ", " ██████ "],
    "F": [" ██████ ", " █      ", " █████  ", " █      ", " █      "],
    "G": [" █████  ", " █      ", " ██  █  ", " █   █  ", " █████  "],
    "H": [" █   █  ", " █   █  ", " █████  ", " █   █  ", " █   █  "],
    "I": [" ███ ", "  █  ", "  █  ", "  █  ", " ███ "],
    "J": ["  ███ ", "   █  ", "   █  ", " █  █ ", "  ██  "],
    "K": [" █  █  ", " █ █   ", " ██    ", " █ █   ", " █  █  "],
    "L": [" █     ", " █     ", " █     ", " █     ", " █████ "],
    "M": [" █    █ ", " ██  ██ ", " █ █ █  ", " █   █  ", " █   █  "],
    "N": [" █   █ ", " ██  █ ", " █ █ █ ", " █  ██ ", " █   █ "],
    "O": [" █████ ", " █   █ ", " █   █ ", " █   █ ", " █████ "],
    "P": [" █████ ", " █   █ ", " █████ ", " █     ", " █     "],
    "Q": [" █████ ", " █   █ ", " █ █ █ ", " █   █ ", "  ██ █ "],
    "R": [" █████ ", " █   █ ", " █████ ", " █  █  ", " █   █ "],
    "S": [" █████ ", " █     ", " ████  ", "     █ ", " █████ "],
    "T": [" ██████ ", "   █   ", "   █   ", "   █   ", "   █   "],
    "U": [" █   █ ", " █   █ ", " █   █ ", " █   █ ", " █████ "],
    "V": [" █   █ ", " █   █ ", " █   █ ", "  █ █  ", "   ██  "],
    "W": [" █   █ ", " █   █ ", " █ █ █ ", " ██ ██ ", " █   █ "],
    "X": [" █   █ ", " █   █ ", "  ███  ", " █   █ ", " █   █ "],
    "Y": [" █   █ ", " █   █ ", "  ███  ", "   █   ", "   █   "],
    "Z": [" █████ ", "    █  ", "   █   ", "  █    ", " █████ "],
    "0": [" █████ ", " █ ██  ", " █ █ █ ", " ██  █ ", " █████ "],
    "1": ["  █  ", " ██  ", "  █  ", "  █  ", " ███ "],
    "2": [" ████  ", "    █  ", "  ███  ", " █     ", " █████ "],
    "3": [" █████ ", "    █  ", "  ███  ", "    █  ", " █████ "],
    "4": ["  █ █  ", "  █ █  ", " █████ ", "    █  ", "    █  "],
    "5": [" █████ ", " █     ", " █████ ", "    █  ", " █████ "],
    "6": ["  ███  ", " █     ", " █████ ", " █   █ ", " █████ "],
    "7": [" █████ ", "    █  ", "   █   ", "  █    ", "  █    "],
    "8": [" █████ ", " █   █ ", " █████ ", " █   █ ", " █████ "],
    "9": [" █████ ", " █   █ ", "  █████", "    █  ", " █████ "],
    "!": [" █ ", " █ ", " █ ", "   ", " █ "],
    "?": [" ███ ", "   █ ", " ██  ", "   ", " ██  "],
    ".": ["   ", "   ", "   ", " █ ", " █ "],
    "-": ["    ", "    ", " ███ ", "    ", "    "],
    "/": ["    █", "   █ ", "  █  ", " █   ", "█    "],
    "@": [" █████ ", " █ ███ ", " █  █  ", " █  █  ", " █████ "],
    " ": ["   ", "   ", "   ", "   ", "   "],
}


def _figlet(text: str) -> str:
    rows = ["", "", "", "", ""]
    for ch in text.upper():
        g = FIGLET_FONT.get(ch, FIGLET_FONT[" "])
        w = max(len(r) for r in g)
        for i in range(5):
            rows[i] += g[i].ljust(w) + " "
    return "\n".join(r.rstrip() for r in rows)


def _cmd_figlet(shell, args, stdin):
    text = " ".join(args) or "MSERVER"
    return _figlet(text), "", 0


def _cmd_nginx(shell, args, stdin):
    if not args:
        return "nginx/1.25.3 — try: nginx -v | nginx -t | nginx start | nginx stop", "", 1
    a = args[0]
    if a in ("-v", "--version"):
        return "nginx version: nginx/1.25.3", "", 0
    if a == "-t":
        return (
            "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok\n"
            "nginx: configuration file /etc/nginx/nginx.conf test is successful"
        ), "", 0
    if a == "start":
        return shell.run("service nginx start")
    if a == "stop":
        return shell.run("service nginx stop")
    if a == "reload":
        shell.run("service nginx stop")
        return shell.run("service nginx start")
    return f"nginx: unknown option {a!r} (try -v, -t, start, stop)", "", 1


def _cmd_git(shell, args, stdin):
    if not args:
        return "usage: git init | add | commit -m msg | log | status | --version", "", 1
    a = args[0]
    gitdir = shell._join(shell.cwd, ".git")
    if a == "--version":
        return "git version 2.43.0.mserver", "", 0
    if a == "init":
        shell.vos.write(gitdir + "/HEAD", "ref: refs/heads/main\n")
        shell.vos.write(
            gitdir + "/config",
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
            "[init]\n\tdefaultBranch = main\n",
        )
        return f"Initialized empty Git repository in {gitdir}/", "", 0
    if a == "add":
        if not shell.vos.exists(gitdir):
            return "fatal: not a git repository", "", 128
        return "ok: changes staged", "", 0
    if a == "commit":
        if not shell.vos.exists(gitdir):
            return "fatal: not a git repository", "", 128
        msg = args[args.index("-m") + 1] if "-m" in args and args.index("-m") + 1 < len(args) else ""
        if not msg:
            return 'error: commit message required: git commit -m "msg"', "", 1
        sha = f"{random.getrandbits(32):08x}"
        shell.vos.append(gitdir + "/COMMITLOG", f"{sha}  {msg}\n")
        return f"[main {sha}] {msg}", "", 0
    if a == "log":
        f = gitdir + "/COMMITLOG"
        if not shell.vos.exists(f):
            return "fatal: not a git repository (no commits yet)", "", 128
        lines = [l for l in shell.vos.read(f).splitlines() if l.strip()]
        if not lines:
            return "no commits yet", "", 0
        return "\n".join("commit " + ln for ln in reversed(lines)), "", 0
    if a == "status":
        if not shell.vos.exists(gitdir):
            return 'fatal: not a git repository (use "init" to create one)', "", 128
        f = gitdir + "/COMMITLOG"
        commits = len([l for l in shell.vos.read(f).splitlines() if l.strip()]) if shell.vos.exists(f) else 0
        return f"On branch main\n{commits} commit(s)\nnothing to commit, working tree clean", "", 0
    if a == "clone":
        return "fatal: network access is disabled in the vOS sandbox", "", 128
    return f"git: unknown command: {a}", "", 1


def _cmd_ssh(shell, args, stdin):
    if not args or args[0] in ("-h", "--help"):
        return (
            "usage: ssh [-V] [user@]host\n"
            "note: outbound connections are disabled in the vOS sandbox;\n"
            "      run 'service ssh start' to accept incoming sessions."
        ), "", 1 if not args else 0
    if args[0] in ("-V", "--version"):
        return "OpenSSH_9.6p1, OpenSSL 3.0.13", "", 0
    return "ssh: outbound connections are disabled in the vOS sandbox", "", 1


# ------------------------------------------------------------------ packages
def _hello_pkg():
    return Package(
        "hello", "1.0.0", "The classic first program",
        {"/usr/bin/hello": "#!/bin/msh\n# vOS stub provided by the 'hello' package\n"},
        {"hello": ("print a friendly greeting", _cmd_hello)},
    )


def _cowsay_pkg():
    return Package(
        "cowsay", "3.0.1", "Make a cow say things",
        {"/usr/bin/cowsay": "#!/bin/msh\n# vOS stub provided by the 'cowsay' package\n"},
        {"cowsay": ("cowsay <phrase>", _cmd_cowsay)},
    )


def _figlet_pkg():
    return Package(
        "figlet", "2.1.2", "Large ASCII banner text",
        {"/usr/bin/figlet": "#!/bin/msh\n# vOS stub provided by the 'figlet' package\n"},
        {"figlet": ("figlet <text>", _cmd_figlet)},
    )


def _nginx_pkg():
    return Package(
        "nginx", "1.25.3", "High-performance web server (service: nginx)",
        {
            "/usr/sbin/nginx": "#!/bin/msh\n# vOS stub provided by the 'nginx' package\n",
            "/etc/nginx/nginx.conf": (
                "worker_processes 1;\n"
                "events {\n    worker_connections 512;\n}\n"
                "http {\n    server {\n        listen 80;\n"
                "        server_name mserver;\n        root /srv/www;\n"
                "        index index.html;\n    }\n}\n"
            ),
            "/srv/www/index.html": (
                "<!doctype html>\n<html><head><meta charset='utf-8'>"
                "<title>MServer</title></head>\n"
                "<body style='background:#0b0f14;color:#7ee787;font-family:monospace;'>"
                "<h1>Hello from nginx on MServerOS</h1></body></html>\n"
            ),
        },
        {"nginx": ("nginx -v | -t | start | stop | reload", _cmd_nginx)},
    )


def _git_pkg():
    return Package(
        "git", "2.43.0", "Distributed source control (offline, sandboxed)",
        {"/usr/bin/git": "#!/bin/msh\n# vOS stub provided by the 'git' package\n"},
        {"git": ("git init|add|commit|log|status", _cmd_git)},
    )


def _ssh_pkg():
    return Package(
        "ssh", "9.6p1", "OpenSSH (server mode via 'service ssh start')",
        {"/usr/sbin/sshd": "#!/bin/msh\n# vOS stub provided by the 'ssh' package\n"},
        {"ssh": ("ssh [-V] [user@]host (outbound disabled in sandbox)", _cmd_ssh)},
    )


def build_registry() -> dict[str, Package]:
    pkgs = [
        _hello_pkg(), _cowsay_pkg(), _figlet_pkg(),
        _nginx_pkg(), _git_pkg(), _ssh_pkg(),
    ]
    return {p.name: p for p in pkgs}


def full_registry(vos):
    """Built-in packages plus any the agent authored at runtime.

    User packages are layered on top but can never replace a built-in —
    UserPkgStore.create refuses those names — so shadowing is impossible.
    """
    reg = build_registry()
    try:
        from .userpkg import UserPkgStore
        for name, pkg in UserPkgStore(vos).all().items():
            if name not in reg:
                reg[name] = pkg
    except Exception:
        pass
    return reg
