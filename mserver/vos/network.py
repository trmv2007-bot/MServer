"""The virtual network: interfaces, name resolution, ports and traffic.

Everything here is simulated inside the vOS. No socket is ever opened on the
host — `curl http://mserver/` reaches the vOS's own nginx and is answered out
of `/srv/www` in the virtual filesystem, not off the real internet.

That separation is deliberate. The agent's one path to the real internet is
`web_fetch` (opt-in, `--net`, see `agent/webfetch.py`), which is guarded and
labels its output as untrusted. If `curl` inside the vOS could also reach the
outside world there would be two egress paths with two different sets of
rules, and the weaker one would define the security of the system.

The topology mimics a QEMU user-mode network, which is what a phone running
this will feel like anyway:

    lo      127.0.0.1/8
    eth0    10.0.2.15/24   gateway 10.0.2.2   dns 10.0.2.3
"""
from __future__ import annotations

import hashlib
import re
import time

LOOPBACK = "127.0.0.1"
HOST_IP = "10.0.2.15"
NETMASK = "255.255.255.0"
GATEWAY = "10.0.2.2"
DNS = "10.0.2.3"
BROADCAST = "10.0.2.255"
MAC = "52:54:00:12:34:56"
MTU = 1500

# Which port a service listens on when no config says otherwise.
DEFAULT_PORTS = {"nginx": 80, "ssh": 22}

# Hosts that resolve even without an /etc/hosts entry, as if a DNS server
# upstream knew about them. Keeps `ping example.com` from being a dead end.
VIRTUAL_DNS = {
    "example.com": "93.184.216.34",
    "www.example.com": "93.184.216.34",
    "gateway": GATEWAY,
    "dns": DNS,
}

_LISTEN_RE = re.compile(r"^\s*listen\s+(?:[\d.]+:)?(\d+)", re.M)
_ROOT_RE = re.compile(r"^\s*root\s+(\S+?);", re.M)
_INDEX_RE = re.compile(r"^\s*index\s+(.+?);", re.M)

_STATUS_TEXT = {
    200: "OK", 301: "Moved Permanently", 400: "Bad Request",
    403: "Forbidden", 404: "Not Found", 500: "Internal Server Error",
    502: "Bad Gateway", 503: "Service Unavailable",
}


class NetworkError(Exception):
    pass


def _jitter(seed: str, lo: float, hi: float) -> float:
    """Deterministic pseudo-latency.

    Deliberately not `random`: the same host pinged twice in a session gives
    a stable-ish figure, and tests do not have to tolerate noise.
    """
    h = hashlib.sha256(seed.encode()).digest()
    frac = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
    return round(lo + frac * (hi - lo), 3)


class Network:
    """Name resolution, listening ports, and the virtual HTTP server."""

    def __init__(self, vos):
        self.vos = vos

    # --------------------------------------------------------- interfaces
    def interfaces(self) -> dict:
        return {
            "lo": {
                "ip": LOOPBACK, "netmask": "255.0.0.0", "mac": None,
                "mtu": 65536, "flags": "UP,LOOPBACK,RUNNING", "up": True,
            },
            "eth0": {
                "ip": HOST_IP, "netmask": NETMASK, "mac": MAC,
                "mtu": MTU, "flags": "UP,BROADCAST,RUNNING,MULTICAST",
                "up": True, "broadcast": BROADCAST, "gateway": GATEWAY,
            },
        }

    def local_ips(self) -> set:
        return {LOOPBACK, HOST_IP, "::1", "0.0.0.0"}

    # ------------------------------------------------------------ resolver
    def hosts_map(self) -> dict:
        """Parse /etc/hosts into {hostname: ip}."""
        out = {}
        try:
            text = self.vos.read("/etc/hosts")
        except Exception:
            return out
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            ip = parts[0]
            for name in parts[1:]:
                out.setdefault(name.lower(), ip)
        return out

    def resolve(self, host: str) -> str | None:
        """Hostname -> IP, or None if it does not resolve."""
        if not host:
            return None
        host = host.strip().lower().rstrip(".")
        if re.fullmatch(r"[\d.]+", host) or ":" in host:
            return host
        hosts = self.hosts_map()
        if host in hosts:
            return hosts[host]
        if host in VIRTUAL_DNS:
            return VIRTUAL_DNS[host]
        return None

    def is_local(self, ip: str) -> bool:
        return ip in self.local_ips()

    # ---------------------------------------------------------------- ports
    def nginx_conf(self) -> dict:
        """Read the real nginx.conf out of the vOS.

        The config file has always existed; nothing ever read it. Now
        `listen` and `root` actually decide what happens.
        """
        conf = {"port": 80, "root": "/srv/www", "index": ["index.html"]}
        try:
            text = self.vos.read("/etc/nginx/nginx.conf")
        except Exception:
            return conf
        m = _LISTEN_RE.search(text)
        if m:
            try:
                conf["port"] = int(m.group(1))
            except ValueError:
                pass
        m = _ROOT_RE.search(text)
        if m:
            conf["root"] = m.group(1)
        m = _INDEX_RE.search(text)
        if m:
            conf["index"] = m.group(1).split()
        return conf

    def listeners(self) -> list:
        """Ports currently open, derived from what is actually running."""
        out = []
        for name in sorted(self.vos.services):
            if self.vos.service_state(name) != "running":
                continue
            pid = self.vos.services[name].get("pid", "-")
            if name == "nginx":
                port = self.nginx_conf()["port"]
            else:
                port = DEFAULT_PORTS.get(name)
            if port is None:
                continue
            out.append({"proto": "tcp", "addr": "0.0.0.0", "port": port,
                        "service": name, "pid": pid, "state": "LISTEN"})
        return out

    def listener_on(self, port: int):
        for lis in self.listeners():
            if lis["port"] == port:
                return lis
        return None

    # ----------------------------------------------------------------- ping
    def ping(self, host: str, count: int = 4) -> tuple:
        """Returns (report_text, exit_code)."""
        ip = self.resolve(host)
        if ip is None:
            return f"ping: {host}: Name or service not known", 2
        lines = [f"PING {host} ({ip}) 56(84) bytes of data."]
        times = []
        for seq in range(1, max(1, min(count, 10)) + 1):
            if self.is_local(ip):
                rtt = _jitter(f"{ip}:{seq}", 0.02, 0.09)
            elif ip.startswith("10.0.2."):
                rtt = _jitter(f"{ip}:{seq}", 0.3, 1.2)
            else:
                rtt = _jitter(f"{ip}:{seq}", 12.0, 48.0)
            times.append(rtt)
            lines.append(
                f"64 bytes from {ip}: icmp_seq={seq} ttl=64 time={rtt} ms")
        lines.append(f"\n--- {host} ping statistics ---")
        lines.append(
            f"{len(times)} packets transmitted, {len(times)} received, "
            f"0% packet loss, time {int(sum(times))}ms")
        lines.append(
            f"rtt min/avg/max = {min(times)}/"
            f"{round(sum(times) / len(times), 3)}/{max(times)} ms")
        return "\n".join(lines), 0

    # ----------------------------------------------------------------- http
    def http_get(self, host: str, port: int, path: str) -> dict:
        """Serve a request from the vOS's own web server.

        Returns {status, reason, headers, body}. Raises NetworkError when
        nothing is listening — that is a connection failure, not an HTTP
        response, and callers need to tell those apart.
        """
        ip = self.resolve(host)
        if ip is None:
            raise NetworkError(f"Could not resolve host: {host}")
        if not self.is_local(ip):
            raise NetworkError(
                f"Failed to connect to {host} port {port}: Network is "
                f"unreachable (the vOS network is virtual; only hosts inside "
                f"it are reachable)")
        lis = self.listener_on(port)
        if lis is None:
            raise NetworkError(
                f"Failed to connect to {host} port {port}: Connection refused")
        if lis["service"] != "nginx":
            raise NetworkError(
                f"Failed to connect to {host} port {port}: "
                f"{lis['service']} is not an HTTP server")

        conf = self.nginx_conf()
        docroot = conf["root"].rstrip("/") or "/srv/www"
        path = (path or "/").split("?", 1)[0]
        if ".." in path:
            return self._resp(400, "text/html", "<h1>400 Bad Request</h1>\n")

        target = docroot + "/" + path.lstrip("/")
        target = re.sub(r"/+", "/", target)

        try:
            if self.vos.is_dir(target):
                for idx in conf["index"]:
                    cand = re.sub(r"/+", "/", f"{target}/{idx}")
                    if self.vos.exists(cand) and not self.vos.is_dir(cand):
                        target = cand
                        break
                else:
                    return self._resp(403, "text/html",
                                      "<h1>403 Forbidden</h1>\n<hr>nginx/1.25.3\n")
            if not self.vos.exists(target):
                return self._resp(404, "text/html",
                                  "<h1>404 Not Found</h1>\n<hr>nginx/1.25.3\n")
            body = self.vos.read(target)
        except NetworkError:
            raise
        except Exception:
            return self._resp(500, "text/html",
                              "<h1>500 Internal Server Error</h1>\n")

        return self._resp(200, _ctype(target), body)

    @staticmethod
    def _resp(status: int, ctype: str, body: str) -> dict:
        return {
            "status": status,
            "reason": _STATUS_TEXT.get(status, "Unknown"),
            "headers": {
                "Server": "nginx/1.25.3",
                "Date": time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                      time.gmtime()),
                "Content-Type": ctype,
                "Content-Length": str(len(body.encode("utf-8", "replace"))),
                "Connection": "close",
            },
            "body": body,
        }


_CTYPES = {
    "html": "text/html", "htm": "text/html", "css": "text/css",
    "js": "application/javascript", "json": "application/json",
    "txt": "text/plain", "md": "text/markdown", "xml": "application/xml",
    "conf": "text/plain", "log": "text/plain",
}


def _ctype(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _CTYPES.get(ext, "application/octet-stream")


def parse_url(url: str) -> tuple:
    """(host, port, path) from a URL. Bare 'mserver/x' is treated as http."""
    raw = url.strip()
    scheme = "http"
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
        scheme = scheme.lower()
    if scheme not in ("http", "https"):
        raise NetworkError(f"unsupported protocol: {scheme}")
    hostpart, _, path = raw.partition("/")
    path = "/" + path
    host, _, portstr = hostpart.partition(":")
    if portstr:
        try:
            port = int(portstr)
        except ValueError:
            raise NetworkError(f"invalid port: {portstr}") from None
    else:
        port = 443 if scheme == "https" else 80
    if not host:
        raise NetworkError("no host in URL")
    return host, port, path
