"""Users, groups and permissions.

This adds a second, *inner* access-control layer. It does not replace the
sandbox — `vpath()` still refuses anything outside the rootfs, and it runs
first. Permissions decide what a vOS user may touch *within* the rootfs; the
sandbox decides what the process may touch on the real machine. Collapsing
the two would be a mistake: a bug in mode-bit arithmetic would then become a
host filesystem escape.

Why the metadata is not real host permissions
---------------------------------------------
The obvious implementation is `os.chmod` on the backing file. Rejected:

* The rootfs lives in the user's home directory. Making files genuinely
  unreadable would leave a phone owner unable to clean up their own data,
  and `pkg install` would start failing in ways nothing could explain.
* Snapshots are directory copies. Host modes survive inconsistently across
  filesystems, and on Android's sdcard mounts they largely do not exist.
* Running MServer as root on the host would make host modes meaningless
  anyway, so the simulation would be a no-op exactly where it matters.

So ownership and mode bits are metadata in `/var/lib/vos/permissions.json`,
inside the rootfs, and therefore covered by snapshot and rollback.

The point of all this
---------------------
`su agent` gives the agent a non-root account that genuinely cannot write to
`/etc`. That is a real reduction in blast radius, and unlike the confirmation
gate it does not depend on a model choosing to behave.
"""
from __future__ import annotations

import json
import re

PASSWD_PATH = "/etc/passwd"
GROUP_PATH = "/etc/group"
PERMS_PATH = "/var/lib/vos/permissions.json"

NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

ROOT_UID = 0
FIRST_UID = 1000

# Default modes when nothing is recorded. Matches a normal Linux install
# closely enough that `ls -l` looks right.
DEFAULT_FILE_MODE = 0o644
DEFAULT_DIR_MODE = 0o755

# Paths that are world-writable on a real system.
_WORLD_WRITABLE = ("/tmp", "/var/tmp")

# Seed accounts. 'agent' exists so the agent can be dropped out of root
# without the user having to set anything up.
DEFAULT_USERS = [
    {"name": "root", "uid": 0, "gid": 0, "home": "/root", "shell": "/bin/msh",
     "gecos": "root"},
    {"name": "agent", "uid": 1000, "gid": 1000, "home": "/home/agent",
     "shell": "/bin/msh", "gecos": "MServer agent"},
    {"name": "nobody", "uid": 65534, "gid": 65534, "home": "/nonexistent",
     "shell": "/usr/sbin/nologin", "gecos": "nobody"},
]


class UserError(Exception):
    pass


class PermissionDenied(Exception):
    """Raised when the current vOS user may not do this.

    Deliberately distinct from VOSPathError (sandbox escape). They mean very
    different things and must never be conflated in a message to the user.
    """


def mode_str(mode: int, is_dir: bool = False) -> str:
    """0o755 -> 'rwxr-xr-x', with a leading d for directories."""
    out = "d" if is_dir else "-"
    for shift in (6, 3, 0):
        bits = (mode >> shift) & 0o7
        out += "r" if bits & 0o4 else "-"
        out += "w" if bits & 0o2 else "-"
        out += "x" if bits & 0o1 else "-"
    return out


def parse_mode(spec: str, current: int = 0o644, is_dir: bool = False) -> int:
    """Accept 755 or symbolic forms like u+x, go-w, a=r."""
    spec = (spec or "").strip()
    if not spec:
        raise UserError("no mode given")
    if re.fullmatch(r"[0-7]{3,4}", spec):
        return int(spec, 8) & 0o7777
    mode = current
    for clause in spec.split(","):
        m = re.fullmatch(r"([ugoa]*)([-+=])([rwx]*)", clause.strip())
        if not m:
            raise UserError(f"invalid mode: {spec!r} (try 755 or u+x)")
        who, op, perms = m.groups()
        who = who or "a"
        if "a" in who:
            who = "ugo"
        bits = 0
        if "r" in perms:
            bits |= 0o4
        if "w" in perms:
            bits |= 0o2
        if "x" in perms:
            bits |= 0o1
        for ch, shift in (("u", 6), ("g", 3), ("o", 0)):
            if ch not in who:
                continue
            if op == "+":
                mode |= bits << shift
            elif op == "-":
                mode &= ~(bits << shift)
            else:
                mode = (mode & ~(0o7 << shift)) | (bits << shift)
    return mode & 0o7777


def _norm(path: str) -> str:
    p = "/" + str(path).strip().lstrip("/")
    while "//" in p:
        p = p.replace("//", "/")
    return p.rstrip("/") or "/"


class UserDB:
    """Accounts, groups, ownership and mode bits for the vOS.

    Reentrancy note
    ---------------
    This class must never read its own metadata through the *checked*
    filesystem API. `vos.read()` calls `check()`, `check()` needs
    `/etc/passwd` and `permissions.json` to decide, and reading those would
    call `check()` again — unbounded recursion, which is exactly what
    happened the first time this was wired up. `_raw_read()` bypasses the
    permission layer (but NOT the sandbox: it still goes through vpath()).
    """

    def __init__(self, vos):
        self.vos = vos
        self.current = "root"       # who is logged in right now
        self._stack: list = []      # su/exit history
        self.enforce = False        # off until someone becomes non-root

    def _raw_read(self, path: str) -> str:
        """Read without permission checks. Sandbox still applies."""
        f = self.vos.vpath(path)
        if not f.exists() or f.is_dir():
            raise FileNotFoundError(path)
        return f.read_text(encoding="utf-8", errors="replace")

    def _raw_write(self, path: str, text: str) -> None:
        f = self.vos.vpath(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    def _raw_is_dir(self, path: str) -> bool:
        try:
            return self.vos.vpath(path).is_dir()
        except Exception:
            return False

    def _raw_exists(self, path: str) -> bool:
        try:
            return self.vos.vpath(path).exists()
        except Exception:
            return False

    # ------------------------------------------------------------ accounts
    def ensure_seeded(self) -> None:
        """Write /etc/passwd and /etc/group if they are missing or stale."""
        try:
            if not self._raw_exists(PASSWD_PATH) or len(self.users()) < 2:
                self._write_passwd(DEFAULT_USERS)
            if not self._raw_exists(GROUP_PATH):
                self._write_group([
                    {"name": u["name"], "gid": u["gid"], "members": []}
                    for u in DEFAULT_USERS
                ])
        except Exception:
            pass

    def users(self) -> list:
        out = []
        try:
            text = self._raw_read(PASSWD_PATH)
        except Exception:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            f = line.split(":")
            if len(f) < 7:
                continue
            try:
                uid, gid = int(f[2]), int(f[3])
            except ValueError:
                continue
            out.append({"name": f[0], "uid": uid, "gid": gid,
                        "gecos": f[4], "home": f[5], "shell": f[6]})
        return out

    def _write_passwd(self, users: list) -> None:
        self._raw_write(PASSWD_PATH, "".join(
            f"{u['name']}:x:{u['uid']}:{u['gid']}:{u.get('gecos','')}:"
            f"{u['home']}:{u['shell']}\n" for u in users))

    def groups(self) -> list:
        out = []
        try:
            text = self._raw_read(GROUP_PATH)
        except Exception:
            return out
        for line in text.splitlines():
            f = line.strip().split(":")
            if len(f) < 4:
                continue
            try:
                gid = int(f[2])
            except ValueError:
                continue
            out.append({"name": f[0], "gid": gid,
                        "members": [m for m in f[3].split(",") if m]})
        return out

    def _write_group(self, groups: list) -> None:
        self._raw_write(GROUP_PATH, "".join(
            f"{g['name']}:x:{g['gid']}:{','.join(g.get('members', []))}\n"
            for g in groups))

    def get_user(self, name: str):
        for u in self.users():
            if u["name"] == name:
                return u
        return None

    def uid_of(self, name: str) -> int:
        u = self.get_user(name)
        return u["uid"] if u else 65534

    def name_of_uid(self, uid: int) -> str:
        for u in self.users():
            if u["uid"] == uid:
                return u["name"]
        return str(uid)

    def add_user(self, name: str, home: str | None = None,
                 shell: str = "/bin/msh") -> dict:
        if not NAME_RE.match(name or ""):
            raise UserError(
                f"invalid user name {name!r}: lowercase letters, digits, "
                f"dash and underscore; must start with a letter")
        if self.get_user(name):
            raise UserError(f"user '{name}' already exists")
        users = self.users()
        uid = max([u["uid"] for u in users if u["uid"] < 65000] or
                  [FIRST_UID - 1]) + 1
        uid = max(uid, FIRST_UID)
        user = {"name": name, "uid": uid, "gid": uid,
                "gecos": "", "home": home or f"/home/{name}", "shell": shell}
        users.append(user)
        self._write_passwd(users)
        groups = self.groups()
        groups.append({"name": name, "gid": uid, "members": []})
        self._write_group(groups)
        # Give them a home they actually own.
        try:
            self.vos.mkdir(user["home"])
            self.set_owner(user["home"], uid, uid)
            self.set_mode(user["home"], 0o755)
        except Exception:
            pass
        self.vos.syslog.write("useradd", f"new user: {name} (uid {uid})")
        return user

    def del_user(self, name: str) -> None:
        if name == "root":
            raise UserError("refusing to delete root")
        if not self.get_user(name):
            raise UserError(f"no such user: {name}")
        if name == self.current:
            raise UserError(f"cannot delete the current user ({name})")
        self._write_passwd([u for u in self.users() if u["name"] != name])
        self._write_group([g for g in self.groups() if g["name"] != name])
        self.vos.syslog.write("userdel", f"deleted user: {name}")

    # ------------------------------------------------------------- session
    def is_root(self) -> bool:
        return self.current == "root"

    def su(self, name: str) -> dict:
        """Switch user.

        Anyone may become a *less* privileged user. Only root may become
        another user without a password, which mirrors real `su` closely
        enough — there are no passwords in the vOS, so the honest rule is
        "you cannot climb back up except by exiting the shell you dropped
        from".
        """
        user = self.get_user(name)
        if not user:
            raise UserError(f"user '{name}' does not exist")
        if not self.is_root() and name == "root":
            raise UserError(
                "su: Authentication failure (only root may become root; "
                "use 'exit' to return to the shell you came from)")
        self._stack.append(self.current)
        self.current = name
        if name != "root":
            self.enforce = True     # permissions start mattering
        self.vos.syslog.auth(f"session opened for user {name}")
        return user

    def exit_user(self) -> str | None:
        if not self._stack:
            return None
        self.current = self._stack.pop()
        self.vos.syslog.auth(f"session closed, back to {self.current}")
        return self.current

    def depth(self) -> int:
        return len(self._stack)

    # --------------------------------------------------------- permissions
    def _load(self) -> dict:
        try:
            data = json.loads(self._raw_read(PERMS_PATH))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        try:
            self._raw_write(PERMS_PATH, json.dumps(data, indent=1,
                                                   sort_keys=True) + "\n")
        except Exception:
            pass

    def meta(self, path: str) -> dict:
        """Recorded owner/mode for a path, or sensible defaults."""
        path = _norm(path)
        rec = self._load().get(path)
        is_dir = self._raw_is_dir(path)
        if rec:
            return {
                "uid": rec.get("uid", 0), "gid": rec.get("gid", 0),
                "mode": rec.get("mode",
                                DEFAULT_DIR_MODE if is_dir else DEFAULT_FILE_MODE),
            }
        # Unrecorded: root-owned, standard mode. Home directories belong to
        # the user they are named for, so a fresh account is usable.
        uid = gid = 0
        for u in self.users():
            if path == _norm(u["home"]) or path.startswith(_norm(u["home"]) + "/"):
                uid = gid = u["uid"]
                break
        return {"uid": uid, "gid": gid,
                "mode": DEFAULT_DIR_MODE if is_dir else DEFAULT_FILE_MODE}

    def set_owner(self, path: str, uid: int, gid: int | None = None) -> None:
        path = _norm(path)
        data = self._load()
        rec = data.get(path, {})
        rec["uid"] = uid
        rec["gid"] = uid if gid is None else gid
        rec.setdefault("mode", self.meta(path)["mode"])
        data[path] = rec
        self._save(data)

    def set_mode(self, path: str, mode: int) -> None:
        path = _norm(path)
        data = self._load()
        rec = data.get(path, {})
        rec["mode"] = mode & 0o7777
        cur = self.meta(path)
        rec.setdefault("uid", cur["uid"])
        rec.setdefault("gid", cur["gid"])
        data[path] = rec
        self._save(data)

    def forget(self, path: str) -> None:
        """Drop metadata for a deleted path, so it cannot haunt a new one."""
        path = _norm(path)
        data = self._load()
        gone = [k for k in data if k == path or k.startswith(path + "/")]
        if gone:
            for k in gone:
                data.pop(k, None)
            self._save(data)

    # ------------------------------------------------------------- checks
    def _can(self, path: str, want: str) -> bool:
        """Does the current user have r/w/x on this path?"""
        if not self.enforce or self.is_root():
            return True
        path = _norm(path)
        if path.startswith(_WORLD_WRITABLE):
            return True
        m = self.meta(path)
        user = self.get_user(self.current)
        uid = user["uid"] if user else 65534
        gid = user["gid"] if user else 65534
        mode = m["mode"]
        if uid == m["uid"]:
            bits = (mode >> 6) & 0o7
        elif gid == m["gid"]:
            bits = (mode >> 3) & 0o7
        else:
            bits = mode & 0o7
        need = {"r": 0o4, "w": 0o2, "x": 0o1}[want]
        return bool(bits & need)

    def check(self, path: str, want: str, op: str = "access") -> None:
        """Raise PermissionDenied unless the current user may do this.

        Writing also requires write permission on the parent directory, which
        is what stops a non-root user creating new files in /etc.
        """
        if not self.enforce or self.is_root():
            return
        path = _norm(path)
        if want == "w":
            target = path if self._raw_exists(path) else _parent(path)
            if not self._can(target, "w"):
                raise PermissionDenied(
                    f"{op}: {path}: Permission denied "
                    f"(you are '{self.current}', not root)")
        elif not self._can(path, want):
            raise PermissionDenied(
                f"{op}: {path}: Permission denied "
                f"(you are '{self.current}', not root)")


def _parent(path: str) -> str:
    p = _norm(path)
    if p == "/":
        return "/"
    return _norm(p.rsplit("/", 1)[0] or "/")
