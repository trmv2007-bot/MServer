"""Web dashboard for MServerOS (stdlib http.server, runs in a thread).

Surfaces:
  /         — status page, updated live over SSE
  /chat     — chat box that talks to the same agent (token-gated)
  /term     — web terminal: a real msh prompt in the browser (token-gated)
  /events   — Server-Sent Events stream of tool calls and shell activity
  /api/status — JSON snapshot of the same data the status page shows
"""
from __future__ import annotations

import hmac
import html
import json
import queue as _queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..vos import packages as pkgmod
from ..vos.kernel import OS_NAME, OS_VERSION, SHELL_VERSION
from . import events as events_mod
from .events import EventBus, LiveEvents, sse_format


class RateLimiter:
    """Per-client rate limiting for the chat endpoint.

    The dashboard binds 0.0.0.0 so other devices on the LAN can watch it,
    which means the chat endpoint is reachable by anything on the network.
    Two separate budgets:

    * a request budget, so one client cannot pin the agent (each turn can
      run tools and cost API credits), and
    * a stricter failure budget, because the realistic attack on this
      endpoint is guessing the token.
    """

    def __init__(self, max_requests: int = 20, window: float = 60.0,
                 max_failures: int = 5, lockout: float = 300.0):
        self.max_requests = max_requests
        self.window = window
        self.max_failures = max_failures
        self.lockout = lockout
        self._hits: dict[str, list[float]] = {}
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, store: dict, client: str, window: float) -> list:
        now = time.time()
        kept = [t for t in store.get(client, []) if now - t < window]
        if kept:
            store[client] = kept
        else:
            store.pop(client, None)
        return kept

    def check(self, client: str) -> tuple[bool, int]:
        """Record a request. Returns (allowed, seconds_to_wait)."""
        with self._lock:
            now = time.time()
            fails = self._prune(self._fails, client, self.lockout)
            if len(fails) >= self.max_failures:
                return False, int(self.lockout - (now - fails[0])) + 1
            hits = self._prune(self._hits, client, self.window)
            if len(hits) >= self.max_requests:
                return False, int(self.window - (now - hits[0])) + 1
            self._hits.setdefault(client, []).append(now)
            return True, 0

    def fail(self, client: str) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(time.time())

    def reset(self, client: str | None = None) -> None:
        with self._lock:
            if client is None:
                self._hits.clear()
                self._fails.clear()
            else:
                self._hits.pop(client, None)
                self._fails.pop(client, None)


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
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
   <tr><td class="big"><a href="/term">open the web terminal →</a></td></tr>
   <tr><th>Session</th><td class="dim">chat and terminal are token-gated (token is printed in the terminal)</td></tr>
   <tr><th>Live</th><td class="dim"><span id="live">connecting…</span></td></tr>
  </table>
 </div>
 <div class="card"><h2>System</h2><table>{sys_rows}</table></div>
 <div class="card"><h2>Processes</h2><table>{ps_rows}</table></div>
 <div class="card"><h2>Storage</h2><table>{disk_rows}</table></div>
 <div class="card"><h2>Packages</h2><table>{pkg_rows}</table></div>
 <div class="card"><h2>Live activity</h2><pre id="feed" class="dim">waiting for events…</pre></div>
 <div class="card"><h2>Recent commands</h2><pre>{recent}</pre></div>
 <div class="card"><h2>Artifacts ({n_art})</h2><table>{art_rows}</table></div>
</div>
<script>
// Live updates over SSE. The page used to reload itself every 5 seconds,
// which threw away scroll position and re-rendered everything to change one
// number. Now only the parts that change are touched.
(function(){{
  var feed=document.getElementById('feed'), live=document.getElementById('live');
  var lines=[];
  function esc(s){{var d=document.createElement('div');d.textContent=s;return d.innerHTML;}}
  function push(t){{
    lines.push(t); if(lines.length>14) lines.shift();
    feed.innerHTML=lines.join('\\n'); feed.className='';
  }}
  try{{
    var es=new EventSource('/events');
    es.addEventListener('hello',function(){{live.textContent='streaming';live.className='ok';}});
    es.addEventListener('shell',function(e){{
      var d=JSON.parse(e.data); push('$ '+esc(d.command||''));}});
    es.addEventListener('shell-result',function(e){{
      var d=JSON.parse(e.data);
      if(d.code) push('  [exit '+d.code+']');}});
    es.addEventListener('tool',function(e){{
      var d=JSON.parse(e.data); push('\\u2699 '+esc(d.name||'tool'));}});
    es.addEventListener('chat',function(e){{
      var d=JSON.parse(e.data);
      push((d.role==='user'?'\\u203a ':'\\u2190 ')+esc((d.message||'').slice(0,120)));}});
    es.onerror=function(){{live.textContent='reconnecting…';live.className='dim';}};
  }}catch(err){{live.textContent='no SSE support';}}
  // Numbers still need refreshing; poll the JSON rather than the whole page.
  setInterval(function(){{
    fetch('/api/status').then(function(r){{return r.json();}}).then(function(j){{
      document.title=j.hostname+' · '+j.uptime;
    }}).catch(function(){{}});
  }}, 10000);
}})();
</script>
</body></html>"""

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


TERM_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MServerOS — terminal</title>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;background:#0b0f14;color:#c9d1d9;
      font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
 header{padding:10px 14px;border-bottom:1px solid #1d2530;
        display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 h1{font-size:14px;margin:0;color:#7ee787}
 .dim{color:#6b7684}
 .pill{border:1px solid #1d2530;border-radius:99px;padding:1px 9px;font-size:12px}
 #log{padding:12px 14px;white-space:pre-wrap;word-break:break-word;
      min-height:60vh}
 .cmd{color:#7ee787}
 .err{color:#ff7b72}
 .code{color:#d29922}
 form{position:sticky;bottom:0;background:#0b0f14;border-top:1px solid #1d2530;
      display:flex;gap:8px;padding:10px 14px}
 #ps1{color:#7ee787;white-space:nowrap}
 input{flex:1;background:#0d1117;border:1px solid #1d2530;border-radius:6px;
       color:#c9d1d9;padding:8px 10px;font:inherit}
 button{background:#238636;border:0;border-radius:6px;color:#fff;
        padding:8px 14px;font:inherit;cursor:pointer}
 a{color:#58a6ff}
</style></head><body>
<header>
  <h1>msh — web terminal</h1>
  <span class="pill dim">__MODE__</span>
  <span class="pill dim" id="who">root</span>
  <span class="dim" style="margin-left:auto">
    <a href="/">status</a> · <a href="/chat">chat</a>
  </span>
</header>
<div id="log"><span class="dim">Type a command. This is the virtual OS \u2014 \
the sandbox, permissions and the confirmation gate all still apply.
It is not a shell on the phone itself.</span>\n\n</div>
<form id="f">
  <span id="ps1">/ $</span>
  <input id="i" autocomplete="off" autocapitalize="off" autocorrect="off"
         spellcheck="false" placeholder="ls -la /">
  <button>run</button>
</form>
<script>
const log=document.getElementById('log'), inp=document.getElementById('i');
const ps1=document.getElementById('ps1'), who=document.getElementById('who');
const token=new URLSearchParams(location.search).get('token')||'';
const hist=[]; let hp=0;
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function add(html){log.insertAdjacentHTML('beforeend',html);
                   window.scrollTo(0,document.body.scrollHeight);}
inp.addEventListener('keydown',e=>{
  if(e.key==='ArrowUp'){if(hp>0){hp--;inp.value=hist[hp]||'';}e.preventDefault();}
  if(e.key==='ArrowDown'){if(hp<hist.length-1){hp++;inp.value=hist[hp]||'';}
                          else{hp=hist.length;inp.value='';}e.preventDefault();}
});
document.getElementById('f').addEventListener('submit',async e=>{
  e.preventDefault();
  const cmd=inp.value.trim(); if(!cmd) return;
  hist.push(cmd); hp=hist.length; inp.value='';
  add('<span class="cmd">'+esc(ps1.textContent+' '+cmd)+'</span>\n');
  if(cmd==='clear'){log.innerHTML='';return;}
  try{
    const r=await fetch('/term',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token,command:cmd})});
    const j=await r.json();
    if(!j.ok){add('<span class="err">'+esc(j.error||'error')+'</span>\n');}
    else{
      if(j.out) add(esc(j.out)+'\n');
      if(j.err) add('<span class="err">'+esc(j.err)+'</span>\n');
      if(j.code) add('<span class="code">[exit '+j.code+']</span>\n');
      ps1.textContent=(j.cwd||'/')+' $'; who.textContent=j.user||'root';
    }
  }catch(err){add('<span class="err">network error</span>\n');}
  add('\n');
});
inp.focus();
</script></body></html>"""


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
        self.limiter = RateLimiter()
        # A separate, roomier budget for the terminal: typing 20 commands in
        # a minute is normal use, whereas 20 chat turns is not.
        self.term_limiter = RateLimiter(max_requests=60, window=60.0)
        self.bus = EventBus()
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

            def _authed(self):
                """Token from the query string, for GET endpoints."""
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                given = (q.get("token") or [""])[0]
                return hmac.compare_digest(str(given), dash.token)

            def _sse(self):
                """Stream events until the client goes away."""
                q = dash.bus.subscribe()
                if q is None:
                    self._json(503, {"ok": False, "error": "too many streams"})
                    return
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    # Without this, a proxy may buffer the stream forever.
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(sse_format(
                        {"seq": 0, "kind": "hello",
                         "data": {"message": "connected"}}))
                    self.wfile.flush()
                    while True:
                        try:
                            ev = q.get(timeout=events_mod.HEARTBEAT_SECONDS)
                            self.wfile.write(sse_format(ev))
                        except _queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass          # the tab was closed; entirely normal
                finally:
                    dash.bus.unsubscribe(q)

            def do_GET(self):
                route = self.path.split("?")[0]
                if route in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", dash.page().encode("utf-8"))
                elif route == "/events":
                    self._sse()
                elif route == "/api/status":
                    self._json(200, dash.status())
                elif route == "/term":
                    page = TERM_PAGE.replace("__MODE__", html.escape(dash.mode_label))
                    self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
                elif route == "/chat":
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

            def _do_term(self):
                """Run one shell command typed into the web terminal.

                This is the same msh the REPL uses, so the sandbox, the
                permission layer and the confirmation gate all still apply.
                It is NOT a shell on the host: there is no host command
                execution path anywhere in this handler.
                """
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    self._json(400, {"ok": False, "error": "bad JSON body"})
                    return
                client = self.client_address[0] if self.client_address else "?"
                allowed, retry_in = dash.term_limiter.check(client)
                if not allowed:
                    self._json(429, {"ok": False,
                                     "error": f"too many commands — wait {retry_in}s"})
                    return
                if not isinstance(body, dict) or not hmac.compare_digest(
                        str(body.get("token", "")), dash.token):
                    dash.term_limiter.fail(client)
                    dash.log_auth(f"failed terminal auth from {client}")
                    self._json(401, {"ok": False, "error": "bad token"})
                    return
                command = str(body.get("command", "")).strip()
                if not command:
                    self._json(400, {"ok": False, "error": "empty command"})
                    return
                try:
                    out, err, code = dash.run_command(command, client)
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"shell error: {e}"})
                    return
                self._json(200, {"ok": True, "out": out, "err": err,
                                 "code": code, "cwd": dash.shell.cwd,
                                 "user": dash.vos.users.current})

            def do_POST(self):
                route = self.path.split("?")[0]
                if route == "/term":
                    self._do_term()
                    return
                if route != "/chat":
                    self._send(404, "text/plain", b"not found")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    self._json(400, {"ok": False, "error": "bad JSON body"})
                    return
                client = self.client_address[0] if self.client_address else "?"
                allowed, retry_in = dash.limiter.check(client)
                if not allowed:
                    self._json(429, {"ok": False,
                                     "error": f"too many requests — wait {retry_in}s"})
                    return
                if not isinstance(body, dict) or not hmac.compare_digest(
                        str(body.get("token", "")), dash.token):
                    # Count failures separately: token guessing is the attack
                    # this endpoint actually faces.
                    dash.limiter.fail(client)
                    dash.log_auth(f"failed chat auth from {client}")
                    self._json(401, {"ok": False, "error": "bad token (it's printed in the terminal)"})
                    return
                message = str(body.get("message", "")).strip()
                if not message:
                    self._json(400, {"ok": False, "error": "empty message"})
                    return
                if dash.agent is None:
                    self._json(503, {"ok": False, "error": "agent not attached"})
                    return
                events = LiveEvents(dash.bus)
                dash.bus.publish("chat", {"role": "user", "message": message})
                try:
                    reply = dash.agent.ask(message, events=events)
                    dash.bus.publish("chat", {"role": "assistant",
                                              "message": reply[:2000]})
                    self._json(200, {"ok": True, "reply": reply,
                                     "events": list(events)})
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

    def status(self) -> dict:
        """JSON snapshot — the same data the status page renders."""
        vos, shell = self.vos, self.shell
        used, files = vos.disk_usage()
        try:
            listeners = vos.network.listeners()
        except Exception:
            listeners = []
        return {
            "os": f"{OS_NAME} {OS_VERSION}",
            "hostname": vos.hostname,
            "uptime": vos.uptime_str(),
            "mode": self.mode_label,
            "user": vos.users.current,
            "disk": {"used_bytes": used, "files": files},
            "processes": [
                {"pid": p["pid"], "cmdline": p["cmdline"],
                 "seconds": int(time.time() - p["started"])}
                for p in (vos.processes[k] for k in sorted(vos.processes))
            ],
            "services": {n: vos.service_state(n) or "stopped"
                         for n in sorted(vos.services)},
            "listeners": listeners,
            "packages": sorted(vos.installed_packages()),
            "recent": [{"at": ts, "command": cmd}
                       for ts, cmd, _h in shell.recent[-12:]],
            "streams": self.bus.subscriber_count(),
        }

    def run_command(self, command: str, client: str = "web") -> tuple:
        """Run one command from the web terminal, and stream it to watchers.

        Uses the same Shell as the REPL on purpose: the point of the web
        terminal is to drive *this* session, so cd and exported variables
        must carry across. Turns are serialised by the agent lock where an
        agent exists, so a command cannot interleave with an agent turn.
        """
        lock = getattr(self.agent, "_lock", None)
        self.bus.publish("shell", {"command": command, "client": client})
        if lock is not None:
            with lock:
                out, err, code = self.shell.run(command)
        else:
            out, err, code = self.shell.run(command)
        self.bus.publish("shell-result", {
            "command": command, "code": code,
            "out": (out or "")[:2000], "err": (err or "")[:2000]})
        try:
            self.vos.syslog.write("mserver-web", f"{client}: {command}")
        except Exception:
            pass
        return out, err, code

    def log_auth(self, message: str) -> None:
        """Record an authentication event in the vOS auth log."""
        try:
            self.vos.syslog.auth(message)
        except Exception:
            pass

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
        registry = pkgmod.full_registry(vos)
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
