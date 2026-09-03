"""Web dashboard for MServerOS (stdlib http.server, runs in a thread).

Two surfaces:
  /      — read-only status page (auto-refresh)
  /chat  — chat box that talks to the same agent (token-gated)
"""
from __future__ import annotations

import html
import hmac
import json
import secrets
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
 .big{{font-size:15px;padding:6px 0}}
</style></head><body>
<h1>{OS_NAME} v{OS_VERSION}</h1>
<div class="sub">{host} · {uptime} · {now} · {mode}</div>
<div class="grid">
 <div class="card"><h2>Agent</h2>
  <table>
   <tr><td class="big"><a href="/chat">talk to the agent →</a></td></tr>
   <tr><th>Session</th><td class="dim">chat is token-gated (token is printed in the terminal)</td></tr>
  </table>
 </div>
 <div class="card"><h2>System</h2><table>{sys_rows}</table></div>
 <div class="card"><h2>Processes</h2><table>{ps_rows}</table></div>
 <div class="card"><h2>Storage</h2><table>{disk_rows}</table></div>
 <div class="card"><h2>Packages</h2><table>{pkg_rows}</table></div>
 <div class="card"><h2>Recent commands</h2><pre>{recent}</pre></div>
 <div class="card"><h2>Artifacts ({n_art})</h2><table>{art_rows}</table></div>
</div></body></html>"""

CHAT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MServerOS · Agent Chat</title>
<style>
 body{background:#0b0f14;color:#c9d1d9;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0}
 .wrap{max-width:780px;margin:0 auto;padding:20px 16px 40px}
 h1{color:#7ee787;font-size:18px;margin:0}
 .sub{color:#8b949e;font-size:12px;margin:4px 0 16px}
 .sub a{color:#58a6ff;text-decoration:none}
 #msgs{min-height:42vh;display:flex;flex-direction:column;gap:8px}
 .msg{border:1px solid #21262d;border-radius:10px;padding:10px 12px;white-space:pre-wrap;word-break:break-word;font-size:13px}
 .you{align-self:flex-end;background:#0d1a12;border-color:#1d3a26;max-width:85%}
 .agent{align-self:flex-start;background:#11161d;max-width:95%}
 .err{border-color:#5a1e1e;color:#f85149}
 .tool{color:#8b949e;font-size:11px;margin:0 0 0 10px}
 .tool b{color:#58a6ff;font-weight:normal}
 .thinking{color:#8b949e;font-size:12px;animation:pulse 1.2s infinite}
 @keyframes pulse{50%{opacity:.4}}
 .bar{display:flex;gap:8px;margin-top:14px}
 input{flex:1;background:#0d1117;border:1px solid #21262d;color:#c9d1d9;border-radius:8px;padding:10px 12px;font:inherit;outline:none}
 input:focus{border-color:#58a6ff}
 button{background:#238636;border:0;color:#fff;border-radius:8px;padding:10px 16px;font:inherit;cursor:pointer}
 button:disabled{opacity:.5;cursor:default}
 .tok{display:flex;gap:10px;align-items:center;margin-bottom:12px}
 .tok input{max-width:240px;font-size:12px;flex:none}
 .tok span{color:#8b949e;font-size:11px}
</style></head>
<body>
<div class="wrap">
 <h1>MServerOS · Agent</h1>
 <div class="sub">__MODE__ · <a href="/">← dashboard</a></div>
 <div class="tok">
  <input type="password" id="token" placeholder="token (printed in the terminal)" autocomplete="off">
  <span>the token gates this chat; the status page stays read-only</span>
 </div>
 <div id="msgs"></div>
 <div class="bar">
  <input type="text" id="msg" placeholder="e.g. install cowsay and say hello to the world" autocomplete="off">
  <button id="send">Send</button>
 </div>
</div>
<script>
var msgs=document.getElementById('msgs'),msg=document.getElementById('msg'),
    send=document.getElementById('send'),tok=document.getElementById('token');
var token=new URLSearchParams(location.search).get('token')||localStorage.getItem('mserver_token')||'';
if(token){tok.value=token;localStorage.setItem('mserver_token',token);}
tok.addEventListener('change',function(){if(tok.value)localStorage.setItem('mserver_token',tok.value);});
function el(cls,txt){var d=document.createElement('div');d.className=cls;d.textContent=txt;
 msgs.appendChild(d);d.scrollIntoView({block:'end'});return d;}
function busy(b){send.disabled=b;msg.disabled=b;}
function addEvent(name,args){var d=document.createElement('div');d.className='tool';
 var b=document.createElement('b');b.textContent=name;
 var parts=[];Object.keys(args||{}).forEach(function(k){
   var v=String(args[k]).replace(/\\n/g,' ');if(v.length>80)v=v.slice(0,79)+'…';
   parts.push(k+'='+v);});
 d.appendChild(document.createTextNode('⚙ '));d.appendChild(b);
 d.appendChild(document.createTextNode(' '+parts.join(' ')));
 msgs.appendChild(d);d.scrollIntoView({block:'end'});}
function sendMsg(){
 var t=tok.value.trim(),m=msg.value.trim();
 if(!t){el('msg agent err','Token missing. The terminal where mserver runs prints the chat link with the token (or set MSERVER_TOKEN).');return;}
 if(!m)return;
 busy(true);msg.value='';
 el('msg you',m);
 var think=el('thinking','agent is working…');
 fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({token:t,message:m})})
 .then(function(r){return r.json().then(function(j){return{ok:r.ok,j:j};});})
 .then(function(x){think.remove();
  if(!x.ok){el('msg agent err',x.j.error||'request failed');return;}
  (x.j.events||[]).forEach(function(e){addEvent(e.name,e.args);});
  el('msg agent',x.j.reply||'(empty reply)');
 })
 .catch(function(e){think.remove();el('msg agent err','network error: '+e);})
 .finally(function(){busy(false);msg.focus();});
}
send.addEventListener('click',sendMsg);
msg.addEventListener('keydown',function(e){if(e.key==='Enter')sendMsg();});
msg.focus();
</script>
</body></html>"""


def _rows(pairs):
    return "".join(f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in pairs)


class Dashboard:
    def __init__(self, vos, shell, artifacts_dir, port: int = 8686, host: str = "0.0.0.0",
                 agent=None, token: str | None = None):
        self.vos = vos
        self.shell = shell
        self.artifacts_dir = artifacts_dir
        self.port = port
        self.host = host
        self.agent = agent
        self.token = token or secrets.token_urlsafe(8)
        self.mode_label = "…"
        self._server = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def actual_port(self) -> int:
        return self._server.server_address[1] if self._server else self.port

    def chat_url(self) -> str:
        return f"http://localhost:{self.port}/chat?token={self.token}"

    # ---------------------------------------------------------------- routes
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

            def _json(self, code, obj):
                self._send(code, "application/json", json.dumps(obj).encode("utf-8"))

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", dash.page().encode("utf-8"))
                elif self.path.split("?")[0] == "/chat":
                    page = CHAT_PAGE.replace("__MODE__", html.escape(dash.mode_label))
                    self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
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

            def do_POST(self):
                if self.path.split("?")[0] != "/chat":
                    self._send(404, "text/plain", b"not found")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    self._json(400, {"ok": False, "error": "bad JSON body"})
                    return
                if not isinstance(body, dict) or not hmac.compare_digest(
                        str(body.get("token", "")), dash.token):
                    self._json(401, {"ok": False, "error": "bad token (it's printed in the terminal)"})
                    return
                message = str(body.get("message", "")).strip()
                if not message:
                    self._json(400, {"ok": False, "error": "empty message"})
                    return
                if dash.agent is None:
                    self._json(503, {"ok": False, "error": "agent not attached"})
                    return
                events: list[dict] = []
                try:
                    reply = dash.agent.ask(message, events=events)
                    self._json(200, {"ok": True, "reply": reply, "events": events})
                except Exception as e:  # never kill the server over one turn
                    self._json(500, {"ok": False, "error": f"agent error: {e}"})

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
