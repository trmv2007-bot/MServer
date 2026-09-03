"""Python version guard.

MServer uses PEP 604 union syntax (``str | None``) and other 3.10+ features.
On an older interpreter the failure mode is an import-time ``TypeError`` deep
inside a module, which tells a Termux user nothing useful. This turns it into
a sentence they can act on.
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 10)


def check_python(exit_on_fail: bool = True) -> bool:
    """Return True if the interpreter is new enough; else explain and exit."""
    if sys.version_info >= MIN_PYTHON:
        return True
    have = ".".join(str(p) for p in sys.version_info[:3])
    need = ".".join(str(p) for p in MIN_PYTHON)
    msg = (
        f"\nMServer needs Python {need} or newer — this is Python {have}.\n\n"
        f"On Termux:\n"
        f"    pkg update && pkg upgrade python\n\n"
        f"Then check with:  python3 --version\n"
    )
    if exit_on_fail:
        print(msg, file=sys.stderr)
        raise SystemExit(1)
    return False
