"""User-authored packages — software the agent writes for itself.

The built-in registry in ``packages.py`` is fixed at import time. This module
adds packages created at runtime, persisted to
``/var/lib/vos/userpkgs.json`` inside the vOS, so a command the agent invents
survives a restart and behaves exactly like a shipped one.

    mserver ❯ write me a command that lists the biggest files, install it
      ⚙ pkg_create name=bigfiles ...
    mserver ❯ !bigfiles /etc

Security: command bodies are **msh script, never Python**
--------------------------------------------------------
It would be far easier to let a package body be Python and ``exec`` it. That
would also be arbitrary code execution on the user's real phone, executed
from LLM output — it would bypass the vOS sandbox completely, because Python
can reach the real filesystem and network.

So a command body is a list of msh lines, run through the same interpreter
and the same sandbox as anything the user types. The worst a malicious
package can do is what the user could already do at the msh prompt, and
destructive shell verbs still go through the confirmation gate.

Bodies get ``$1``…``$9``, ``$@`` (all arguments) and ``$#`` (count) on top of
normal shell expansion.
"""
from __future__ import annotations

import json
import re
import time

STORE_PATH = "/var/lib/vos/userpkgs.json"

MAX_PACKAGES = 64
MAX_BODY_LINES = 60
MAX_FILES = 20
MAX_FILE_BYTES = 64_000

NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")

# Names that must not be shadowed. Letting a package redefine `rm` or `pkg`
# would let one poisoned package quietly rewrite the system underneath the
# user.
PROTECTED = {
    "pkg", "snapshot", "service", "rm", "cd", "help", "reboot", "man",
    "export", "unset", "alias", "unalias", "source", "history", "logger",
}


class UserPkgError(Exception):
    pass


def validate_name(name: str, what: str = "package") -> str:
    if not NAME_RE.match(name or ""):
        raise UserPkgError(
            f"invalid {what} name {name!r}: lowercase letters, digits, "
            f"dot, dash, underscore; must start with a letter; max 32 chars")
    return name


class UserPackage:
    """A package authored at runtime. Mirrors packages.Package's interface."""

    def __init__(self, name, version, description, files=None,
                 commands=None, created=None, author="agent"):
        self.name = name
        self.version = version or "1.0.0"
        self.description = description or ""
        self.files = files or {}
        # {command_name: {"help": str, "body": [msh lines]}}
        self.commands = commands or {}
        self.created = created or time.time()
        self.author = author

    # ------------------------------------------------------------ registry
    def register(self, shell) -> None:
        for cname, spec in self.commands.items():
            shell.register_command(
                cname,
                _make_runner(cname, spec.get("body", [])),
                spec.get("help", f"{cname} (from {self.name})"),
                self.name,
            )

    def unregister(self, shell) -> None:
        for cname in self.commands:
            shell.unregister_command(cname, self.name)

    # -------------------------------------------------------------- codec
    def to_dict(self) -> dict:
        return {
            "name": self.name, "version": self.version,
            "description": self.description, "files": self.files,
            "commands": self.commands, "created": self.created,
            "author": self.author,
        }

    @classmethod
    def from_dict(cls, d: dict) -> UserPackage:
        return cls(
            name=d["name"], version=d.get("version", "1.0.0"),
            description=d.get("description", ""), files=d.get("files") or {},
            commands=d.get("commands") or {}, created=d.get("created"),
            author=d.get("author", "agent"),
        )


def _make_runner(cname: str, body: list):
    """Build the shell callable for a scripted command.

    Every line runs through ``shell.run``, so the sandbox, the path checks
    and the confirmation gate all still apply.
    """
    def run(shell, args, stdin):
        outs, errs, code = [], [], 0
        # Positional arguments, shell style.
        saved = {k: shell.env.get(k) for k in
                 [str(i) for i in range(1, 10)] + ["@", "#"]}
        try:
            for i in range(1, 10):
                shell.env[str(i)] = args[i - 1] if len(args) >= i else ""
            shell.env["@"] = " ".join(args)
            shell.env["#"] = str(len(args))
            for raw in body[:MAX_BODY_LINES]:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                o, e, code = shell.run(line, stdin=stdin, _record=False)
                if o:
                    outs.append(o.rstrip("\n"))
                if e:
                    errs.append(e.rstrip("\n"))
                stdin = None
        finally:
            for k, v in saved.items():
                if v is None:
                    shell.env.pop(k, None)
                else:
                    shell.env[k] = v
        return "\n".join(outs), "\n".join(errs), code

    run.__name__ = f"userpkg_{cname}"
    return run


class UserPkgStore:
    """Loads, validates and persists agent-authored packages."""

    def __init__(self, vos):
        self.vos = vos

    # ---------------------------------------------------------- persistence
    def _read(self) -> dict:
        try:
            if not self.vos.exists(STORE_PATH):
                return {}
            data = json.loads(self.vos.read(STORE_PATH))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        self.vos.write(STORE_PATH, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def all(self) -> dict:
        out = {}
        for name, raw in self._read().items():
            try:
                out[name] = UserPackage.from_dict(raw)
            except Exception:
                continue
        return out

    def get(self, name: str):
        return self.all().get(name)

    def exists(self, name: str) -> bool:
        return name in self._read()

    # --------------------------------------------------------------- create
    def create(self, name, description="", version="1.0.0", files=None,
               commands=None, overwrite=False, builtin_names=(),
               shell_names=()) -> UserPackage:
        """Validate and store a new package."""
        validate_name(name)
        files = files or {}
        commands = commands or {}

        if not files and not commands:
            raise UserPkgError(
                "a package needs at least one file or one command")

        existing = self._read()
        if name in existing and not overwrite:
            raise UserPkgError(
                f"package '{name}' already exists (pass overwrite to replace it)")
        if name in builtin_names:
            raise UserPkgError(f"'{name}' is a built-in package name")
        if len(existing) >= MAX_PACKAGES and name not in existing:
            raise UserPkgError(f"too many user packages (limit {MAX_PACKAGES})")

        if len(files) > MAX_FILES:
            raise UserPkgError(f"too many files (limit {MAX_FILES})")
        for path, content in files.items():
            if not isinstance(content, str):
                raise UserPkgError(f"file {path}: content must be text")
            if len(content) > MAX_FILE_BYTES:
                raise UserPkgError(
                    f"file {path}: too large ({len(content)} > {MAX_FILE_BYTES} bytes)")
            # Reject the path here as well as at write time, so a bad package
            # fails at creation rather than half-installing later.
            self.vos.vpath(path)

        normalised = {}
        for cname, spec in commands.items():
            validate_name(cname, what="command")
            if cname in PROTECTED:
                raise UserPkgError(
                    f"'{cname}' is a protected command and cannot be replaced")
            # A user package may not shadow an existing shell command unless
            # it already owns that name.
            if cname in shell_names:
                raise UserPkgError(
                    f"'{cname}' is already a command; pick another name")
            if isinstance(spec, str):
                spec = {"body": spec.splitlines()}
            body = spec.get("body")
            if isinstance(body, str):
                body = body.splitlines()
            if not body or not isinstance(body, list):
                raise UserPkgError(f"command '{cname}': needs a non-empty body")
            if len(body) > MAX_BODY_LINES:
                raise UserPkgError(
                    f"command '{cname}': body too long "
                    f"({len(body)} > {MAX_BODY_LINES} lines)")
            body = [str(ln) for ln in body]
            _reject_python(cname, body)
            normalised[cname] = {
                "help": str(spec.get("help") or f"{cname} (from {name})")[:120],
                "body": body,
            }

        pkg = UserPackage(name=name, version=str(version or "1.0.0"),
                          description=str(description or "")[:200],
                          files={str(k): str(v) for k, v in files.items()},
                          commands=normalised)
        existing[name] = pkg.to_dict()
        self._write(existing)
        return pkg

    def remove(self, name: str) -> None:
        data = self._read()
        if name not in data:
            raise UserPkgError(f"no such user package: {name}")
        data.pop(name)
        self._write(data)


_PYTHON_SMELL = re.compile(
    r"^\s*(?:import\s+\w|from\s+\w+\s+import|def\s+\w+\s*\(|class\s+\w+"
    r"|__import__|eval\s*\(|exec\s*\()")


def _reject_python(cname: str, body: list) -> None:
    """Catch a model that tried to write Python instead of msh.

    This is a helpfulness guard, not a security boundary: the body is passed
    to the msh interpreter either way and Python is never executed. Without
    it the failure is a confusing 'command not found: import'.
    """
    for line in body:
        if _PYTHON_SMELL.match(line):
            raise UserPkgError(
                f"command '{cname}': body must be msh shell script, not "
                f"Python. Offending line: {line.strip()[:60]!r}. Use shell "
                f"commands like: ls, cat, grep, echo, sort, find.")
