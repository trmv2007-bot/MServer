"""Web dashboard for MServerOS (stdlib http.server, runs in a thread)."""
from __future__ import annotations

import html
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..vos.kernel import OS_NAME, OS_VERSION, SHELL_VERSION
from ..vos import packages as pkgmod

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MServerOS dashboard</title>
<style>
 body{{background:#0b0f14;color:#c9d1d9;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px}}
 h1{{color:#7ee787;font-size:20px;margin:0 0 4px}}
 .sub{{color:#8b949e;font-size:12px;margin-bottom:20px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}}
 .card{{background:#11161d;border:1px solid #21262d;border-radius:10px;padding:14px 16px}}
 .card h2{{font-size:12px;color:#58a6ff;margin:0 0 10px;text-transform:uppercase;letter-spacing:.08em}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 td,th{{text-align:left;padding:3px 10px 3px 0;vertical-align:top}}
 th{{color:#8b949e;font-weight:normal}}
 .ok{{color:#7ee787}}.dim{{color:#8b949e}}
 a{{color:#58a6ff;text-decoration:none}}
 pre{{white-space:pre-wrap;font-size:12px;color:#c9d1d9;margin:0}}
</style></head><body>
<h1>{OS_NAME} v{OS_VERSION}</h1>
<div class="sub">{host} · {uptime} · {now} · {mode}</div>
<div class="grid">
 <div class="card"><h2>System</h2><table>{sys_rows}</table></div>
 <div class="card"><h2>Processes</h2><table>{ps_rows}</table></div>
 <div class="card"><h2>Storage</h2><table>{disk_rows}</table></div>
 <div class="card"><h2>Packages</h2><table>{pkg_rows}</table></div>
 <div class="card"><h2>Recent commands</h2><pre>{recent}</pre></div>
 <div class="card"><h2>Artifacts ({n_art})</h2><table>{art_rows}</table></div>
</div></body></html>"""


def _rows(pairs):
    return "".join(f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in pairs)


class Dashboard:
    def __init__(self, vos, shell, artifacts_dir, port: int = 8686, host: str = "0.0.0.0"):
        self.vos = vos
        self.shell = shell
        self.artifacts_dir = artifacts_dir
        self.port = port
        self.host = host
        self.mode_label = "…"
        self._server = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self.running:
            return
        dash = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", dash.page().encode("utf-8"))
                elif self.path.startswith("/a/"):
                    name = self.path[3:].strip("/")
                    p = dash.artifacts_dir / name
                    if name and "/" not in name and name == p.name and p.is_file() and p.suffix == ".md":
                        self._send(200, "text/markdown; charset=utf-8",
                                   p.read_text("utf-8", "replace").encode("utf-8"))
                    else:
                        self._send(404, "text/plain", b"not found")
                else:
                    self._send(404, "text/plain", b"not found")

        self._server = ThreadingHTTPServer((self.host, self.port), H)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None

    def url(self) -> str:
        return f"http://localhost:{self.port}"

    # ----------------------------------------------------------------- page
    def page(self) -> str:
        vos, shell = self.vos, self.shell
        used, files = vos.disk_usage()
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        sys_rows = _rows([
            ("Host", f"root@{vos.hostname}"),
            ("OS", f"{OS_NAME} {OS_VERSION} (virtual Linux)"),
            ("Kernel", "vos 1.0 / python3"),
            ("Shell", f"msh {SHELL_VERSION}"),
            ("Uptime", html.escape(vos.uptime_str())),
            ("Mode", html.escape(self.mode_label)),
        ])

        ps_rows = "".join(
            f"<tr><td>{p['pid']}</td><td>{html.escape(p['cmdline'])}</td>"
            f"<td class='dim'>{int(time.time() - p['started'])}s</td></tr>"
            for p in (vos.processes[k] for k in sorted(vos.processes))
        ) or "<tr><td class='dim'>none</td></tr>"

        disk_rows = _rows([
            ("Rootfs", f"<code>{html.escape(str(vos.root))}</code>"),
            ("Used", f"{used // 1024} KiB ({files} files)"),
            ("Quota", "sandboxed — device FS is never touched"),
        ])

        installed = set(vos.installed_packages())
        registry = pkgmod.build_registry()
        pkg_rows = "".join(
            f"<tr><td>{html.escape(n)}</td><td class='dim'>{html.escape(registry[n].version)}</td>"
            f"<td class='{'ok' if n in installed else 'dim'}'>{'installed' if n in installed else '—'}</td></tr>"
            for n in sorted(registry)
        )

        recent = "\n".join(
            f"{time.strftime('%H:%M:%S', time.localtime(ts))}  $ {html.escape(cmd)}"
            for ts, cmd, _head in shell.recent[-12:]
        ) or "<span class='dim'>none yet</span>"

        arts = sorted(self.artifacts_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        art_rows = "".join(
            f"<tr><td><a href='/a/{html.escape(a.name)}'>{html.escape(a.name)}</a></td>"
            f"<td class='dim'>{time.strftime('%m-%d %H:%M', time.localtime(a.stat().st_mtime))}</td></tr>"
            for a in arts
        ) or "<tr><td class='dim'>nothing presented yet</td></tr>"

        return PAGE.format(
            OS_NAME=OS_NAME, OS_VERSION=OS_VERSION,
            host=html.escape(vos.hostname),
            uptime=html.escape(vos.uptime_str()), now=now,
            mode=html.escape(self.mode_label),
            sys_rows=sys_rows, ps_rows=ps_rows, disk_rows=disk_rows,
            pkg_rows=pkg_rows, recent=recent,
            n_art=len(arts), art_rows=art_rows,
        )
