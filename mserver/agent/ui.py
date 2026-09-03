"""ANSI terminal UI for MServer (no dependencies, degrades to plain text)."""
from __future__ import annotations

import os
import re
import shutil
import sys

ANSI_RE = re.compile(r"\033\[[0-9;]*m")

LOGO = [
    "    ███╗   ██╗██╗████████╗ █████╗ ██╗     ██████╗  ██████╗  █████╗ ███████╗",
    "    ████╗  ██║██║╚══██╔══╝██╔══██╗██║     ██╔══██╗██╔═══██╗██╔══██╗██╔════╝",
    "    ██╔██╗ ██║██║   ██║   ███████║██║     ██║  ██║██║   ██║███████║█████╗  ",
    "    ██║╚██╗██║██║   ██║   ██╔══██║██║     ██║  ██║██║   ██║██╔══██║██╔══╝  ",
    "    ██║ ╚████║██║   ██║   ██║  ██║███████╗██████╔╝╚██████╔╝██║  ██║███████╗",
    "    ╚═╝  ╚═══╝╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝",
]


class UI:
    def __init__(self):
        self.enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def _c(self, s: str, code: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.enabled else s

    def bold(self, s): return self._c(s, "1")
    def dim(self, s): return self._c(s, "2")
    def red(self, s): return self._c(s, "31")
    def green(self, s): return self._c(s, "32")
    def yellow(self, s): return self._c(s, "33")
    def blue(self, s): return self._c(s, "34")
    def magenta(self, s): return self._c(s, "35")
    def cyan(self, s): return self._c(s, "36")

    def plain(self, s: str) -> str:
        return ANSI_RE.sub("", s)

    def println(self, s: str = "") -> None:
        print(s)

    def banner(self) -> None:
        for line in LOGO:
            print(self.cyan(self.bold(line)))
        print(self.dim("   virtual linux · ai agent · termux — vOS 1.0"))

    def _break_at(self, s: str, width: int) -> int:
        if len(s) <= width:
            return len(s)
        seg = s[:width]
        sp = seg.rfind(" ")
        return sp if sp > width // 2 else width

    def panel(self, title: str, text: str, color: str = "cyan", width: int | None = None) -> None:
        cols = shutil.get_terminal_size((100, 24)).columns
        width = min(width or cols, 78)
        inner = max(20, width - 4)
        c = getattr(self, color, self.cyan)
        wrapped = []
        for raw in str(text).splitlines():
            if not raw:
                wrapped.append("")
                continue
            while len(self.plain(raw)) > inner:
                cut = self._break_at(raw, inner)
                wrapped.append(raw[:cut])
                raw = raw[cut:].lstrip()
            wrapped.append(raw)
        body = wrapped if wrapped else [""]
        title_plain = self.plain(title)
        top = f"╭─ {title} " + "─" * max(0, inner - len(title_plain) - 1) + "╮"
        rows = [c(top)]
        for ln in body[:60]:
            pad = max(0, inner - len(self.plain(ln)))
            rows.append(f"│ {ln}{' ' * pad} │")
        rows.append(c("╰" + "─" * (width - 2) + "╯"))
        print("\n".join(rows))

    def tool_trace(self, name: str, args: dict | None) -> None:
        bits = []
        for k, v in (args or {}).items():
            sv = str(v).replace("\n", " ")
            if len(sv) > 60:
                sv = sv[:59] + "…"
            bits.append(f"{k}={self.dim(sv)}")
        print(self.dim(f"  ⚙ {self.cyan(name)} " + " ".join(bits)))
