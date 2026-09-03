"""Agent tools — the only hands the AI has. Everything is confined to the vOS."""
from __future__ import annotations

import re
import time

MAX_OUT = 4000


def _clip(text: str) -> str:
    if len(text) <= MAX_OUT:
        return text
    return text[:MAX_OUT] + f"\n... [truncated, {len(text)} chars total]"


def fmt_shell(cmd: str, out: str, err: str, code: int) -> str:
    parts = [f"$ {cmd}"]
    if out:
        parts.append(_clip(out.rstrip("\n")))
    if err:
        parts.append("[stderr] " + _clip(err.rstrip("\n")))
    if code != 0:
        parts.append(f"[exit {code}]")
    if not out and not err and code == 0:
        parts.append("[no output]")
    return "\n".join(parts)


def _schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def build_tools(vos, shell, hooks):
    """Build (schemas, executors).

    hooks: {
        "on_present": fn(title, content, filename),
        "dashboard":  fn(action, port) -> str,
    }
    """

    def t_vos_run(a):
        cmd = a.get("command", "")
        if not cmd:
            return "error: empty command"
        out, err, code = shell.run(cmd)
        return fmt_shell(cmd, out, err, code)

    def t_vos_list(a):
        path = a.get("path", "/")
        try:
            entries = shell.vos.listdir(path)
        except Exception as e:
            return f"error: {e}"
        if not entries:
            return f"(empty directory) {path}"
        lines = [f"ls {path}:"]
        for e in entries:
            perm = "d" if e["isdir"] else "-"
            lines.append(f"{perm} {e['name']:<28} {e['size']:>8}")
        return _clip("\n".join(lines))

    def t_vos_read(a):
        try:
            return _clip(vos.read(a.get("path", "/")))
        except Exception as e:
            return f"error: {e}"

    def t_vos_write(a):
        path, content = a.get("path", ""), a.get("content", "")
        if not path:
            return "error: path required"
        try:
            n = vos.write(path, content)
        except Exception as e:
            return f"error: {e}"
        return f"wrote {n} bytes to {path}"

    def t_vos_search(a):
        pattern = a.get("pattern", "")
        if not pattern:
            return "error: pattern required"
        hits = vos.search(pattern, a.get("path", "/"), limit=60)
        if not hits:
            return "no matches"
        return _clip("\n".join(f"{p}:{n}: {t}" for p, n, t in hits))

    def t_vos_delete(a):
        path = a.get("path", "")
        if not path:
            return "error: path required"
        try:
            vos.remove(path)
            return f"removed {path}"
        except Exception as e:
            return f"error: {e}"

    def t_pkg_list(a):
        out, err, _ = shell.run("pkg list")
        return out or err

    def t_pkg_install(a):
        name = a.get("name", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            return "error: bad package name"
        out, err, code = shell.run(f"pkg install {name}")
        return fmt_shell(f"pkg install {name}", out, err, code)

    def t_pkg_remove(a):
        name = a.get("name", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            return "error: bad package name"
        out, err, code = shell.run(f"pkg remove {name}")
        return fmt_shell(f"pkg remove {name}", out, err, code)

    def t_services(a):
        out, err, _ = shell.run("ps")
        return out or err

    def t_present(a):
        title = (a.get("title") or "untitled").strip() or "untitled"
        content = a.get("content") or ""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-").lower()[:40] or "artifact"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        fname = f"{stamp}-{safe}.md"
        try:
            hooks["on_present"](title, content, fname)
            return f"presented '{title}' to the user as {fname}"
        except Exception as e:
            return f"error presenting: {e}"

    def t_dashboard(a):
        try:
            port = int(a.get("port") or 8686)
        except (TypeError, ValueError):
            port = 8686
        return hooks["dashboard"](a.get("action", "status"), port)

    S = lambda d: {"type": "string", "description": d}  # noqa: E731
    schemas = [
        _schema(
            "vos_run",
            "Run a command in the MServerOS shell (msh). Supports pipes |, "
            "redirection > and >>, and globs. Returns stdout/stderr/exit code.",
            {"command": S("msh command line")},
            ["command"],
        ),
        _schema(
            "vos_list", "List a directory in the vOS filesystem.",
            {"path": S("directory, default /")}, [],
        ),
        _schema(
            "vos_read", "Read a text file from the vOS filesystem.",
            {"path": S("virtual path")}, ["path"],
        ),
        _schema(
            "vos_write",
            "Create or overwrite a text file in the vOS (parent dirs are created).",
            {"path": S("virtual path"), "content": S("file content")},
            ["path", "content"],
        ),
        _schema(
            "vos_search", "Search file contents in the vOS (regex, case-insensitive).",
            {"pattern": S("regex pattern"), "path": S("search root, default /")},
            ["pattern"],
        ),
        _schema(
            "vos_delete", "Delete a file or directory in the vOS.",
            {"path": S("virtual path")}, ["path"],
        ),
        _schema("pkg_list", "List all vOS packages and which are installed.", {}, []),
        _schema(
            "pkg_install",
            "Install a vOS package (available: hello, cowsay, figlet, nginx, git, ssh).",
            {"name": S("package name")}, ["name"],
        ),
        _schema(
            "pkg_remove", "Remove a vOS package.",
            {"name": S("package name")}, ["name"],
        ),
        _schema("services", "Show running processes/services in the vOS.", {}, []),
        _schema(
            "present",
            "Present a finished artifact (report, config, table, ASCII art) to the "
            "user in a highlighted panel. Call it exactly once when the task is done.",
            {"title": S("short artifact title"), "content": S("artifact body")},
            ["title", "content"],
        ),
        _schema(
            "dashboard", "Control the web dashboard: status | start | stop.",
            {"action": S("status | start | stop"), "port": {"type": "integer", "description": "default 8686"}},
            ["action"],
        ),
    ]
    executors = {
        "vos_run": t_vos_run,
        "vos_list": t_vos_list,
        "vos_read": t_vos_read,
        "vos_write": t_vos_write,
        "vos_search": t_vos_search,
        "vos_delete": t_vos_delete,
        "pkg_list": t_pkg_list,
        "pkg_install": t_pkg_install,
        "pkg_remove": t_pkg_remove,
        "services": t_services,
        "present": t_present,
        "dashboard": t_dashboard,
    }
    return schemas, executors
