"""Filesystem snapshots for the vOS — the undo button.

The entire virtual OS is one directory, so a snapshot is just a copy of it and
a rollback is a swap. That makes it cheap to implement and it is exactly the
safety net an LLM agent with write access needs: any destructive run can be
undone in full rather than argued with.

Snapshots live *outside* the rootfs (a sibling ``snapshots/`` directory), so
they are invisible to the agent and cannot themselves be deleted with
``rm -rf /``. That placement is the whole point — an undo history the agent
can reach is not an undo history.

Layout::

    ~/.mserver/
      vos/                 <- the rootfs the agent sees as "/"
      snapshots/
        <name>/
          rootfs/          <- the copy
          meta.json        <- label, timestamp, file count, size

Usage from msh::

    snapshot                    list snapshots
    snapshot save before-nginx  take one
    snapshot rollback <name>    restore it
    snapshot rm <name>          delete one
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

# Bounded so a phone cannot fill up with old copies. The oldest auto-snapshot
# is evicted first; named ones the user asked for are kept.
MAX_SNAPSHOTS = 20

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SnapshotError(Exception):
    pass


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


_auto_seq = 0


def _auto_name() -> str:
    """Unique auto-snapshot name.

    A bare timestamp collides when two snapshots are taken inside the same
    second — which happens routinely, e.g. rollback saves an undo point and
    the caller immediately saves another.
    """
    global _auto_seq
    _auto_seq += 1
    return time.strftime("auto-%Y%m%d-%H%M%S-") + f"{_auto_seq:03d}"


class SnapshotStore:
    """Manages snapshots of a VOS rootfs."""

    def __init__(self, vos, base: Path | None = None):
        self.vos = vos
        # Sibling of the rootfs, deliberately outside it.
        self.base = Path(base) if base else vos.root.parent / "snapshots"

    # ------------------------------------------------------------ internals
    def _dir(self, name: str) -> Path:
        return self.base / name

    def _check(self, name: str) -> str:
        if not valid_name(name):
            raise SnapshotError(
                f"invalid snapshot name: {name!r} "
                "(letters, digits, dot, dash, underscore; max 64 chars)")
        return name

    # ---------------------------------------------------------------- query
    def list(self) -> list[dict]:
        """All snapshots, newest first."""
        if not self.base.is_dir():
            return []
        out = []
        for d in self.base.iterdir():
            if not d.is_dir():
                continue
            meta = self._read_meta(d)
            if meta:
                out.append(meta)
        return sorted(out, key=lambda m: m.get("created", 0), reverse=True)

    def exists(self, name: str) -> bool:
        return (self._dir(name) / "rootfs").is_dir()

    def _read_meta(self, d: Path) -> dict | None:
        f = d / "meta.json"
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if not (d / "rootfs").is_dir():
                return None
            meta = {}
        meta.setdefault("name", d.name)
        meta.setdefault("created", 0)
        meta.setdefault("label", "")
        meta.setdefault("files", 0)
        meta.setdefault("bytes", 0)
        return meta

    # ----------------------------------------------------------------- save
    def save(self, name: str | None = None, label: str = "") -> dict:
        """Copy the current rootfs into a new snapshot."""
        # `None` means "pick a name for me"; an empty string is a caller bug
        # and must not be silently turned into an auto-name.
        name = self._check(_auto_name() if name is None else name)
        dest = self._dir(name)
        if dest.exists():
            raise SnapshotError(f"snapshot already exists: {name}")

        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.base / f".tmp-{name}-{int(time.time())}"
        try:
            shutil.copytree(self.vos.root, tmp / "rootfs")
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SnapshotError(f"could not snapshot: {e}") from e

        files = sum(1 for p in (tmp / "rootfs").rglob("*") if p.is_file())
        size = sum(p.stat().st_size for p in (tmp / "rootfs").rglob("*") if p.is_file())
        meta = {"name": name, "label": label, "created": time.time(),
                "files": files, "bytes": size}
        (tmp / "meta.json").write_text(json.dumps(meta, indent=2) + "\n",
                                       encoding="utf-8")
        tmp.rename(dest)
        self._evict()
        return meta

    def _evict(self) -> None:
        """Keep the store bounded; drop the oldest auto-snapshots first."""
        snaps = self.list()
        if len(snaps) <= MAX_SNAPSHOTS:
            return
        autos = [s for s in snaps if s["name"].startswith("auto-")]
        for victim in autos[MAX_SNAPSHOTS - len(snaps):]:
            self.remove(victim["name"], _internal=True)

    # ------------------------------------------------------------- rollback
    def rollback(self, name: str) -> dict:
        """Replace the live rootfs with a snapshot.

        The current state is saved first, so a rollback is itself undoable —
        rolling back by mistake should never be terminal.
        """
        self._check(name)
        src = self._dir(name) / "rootfs"
        if not src.is_dir():
            raise SnapshotError(f"no such snapshot: {name}")

        pre = self.save(label=f"auto-taken before rollback to {name}")

        root = self.vos.root
        staging = root.parent / f".rollback-{int(time.time() * 1000)}"
        old = root.parent / f".old-{int(time.time() * 1000)}"
        try:
            shutil.copytree(src, staging)
            # Swap via rename so a failure cannot leave a half-restored rootfs.
            root.rename(old)
            staging.rename(root)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            if old.is_dir() and not root.is_dir():
                old.rename(root)  # put it back
            raise SnapshotError(f"rollback failed: {e}") from e
        finally:
            shutil.rmtree(old, ignore_errors=True)

        # The kernel caches nothing off-disk except the process table, but the
        # shell's cwd may now point at a directory that no longer exists.
        meta = self._read_meta(self._dir(name)) or {"name": name}
        meta["undo"] = pre["name"]
        return meta

    # --------------------------------------------------------------- delete
    def remove(self, name: str, _internal: bool = False) -> None:
        self._check(name)
        d = self._dir(name)
        if not d.is_dir():
            raise SnapshotError(f"no such snapshot: {name}")
        shutil.rmtree(d, ignore_errors=True)

    # ---------------------------------------------------------------- usage
    def total_bytes(self) -> int:
        return sum(s.get("bytes", 0) for s in self.list())


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} B"


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.1f} MiB"


def fmt_age(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"
