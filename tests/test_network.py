"""Tests for the virtual network: resolution, ports, ping, curl/wget, nginx."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mserver.agent.tools import build_tools  # noqa: E402
from mserver.vos import network as netmod  # noqa: E402
from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402


def new_env(serve=False):
    d = tempfile.mkdtemp()
    vos = VOS(os.path.join(d, ".mserver"))
    shell = Shell(vos)
    if serve:
        shell.run("pkg install nginx")
        shell.run("service nginx start")
    return vos, shell, d


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env()
        self.net = self.vos.network

    def test_localhost_from_etc_hosts(self):
        self.assertEqual(self.net.resolve("localhost"), "127.0.0.1")

    def test_hostname_resolves(self):
        self.assertEqual(self.net.resolve("mserver"), "127.0.0.1")

    def test_case_insensitive(self):
        self.assertEqual(self.net.resolve("MServer"), "127.0.0.1")

    def test_ip_passes_through(self):
        self.assertEqual(self.net.resolve("10.0.2.15"), "10.0.2.15")

    def test_virtual_dns_fallback(self):
        self.assertEqual(self.net.resolve("example.com"), "93.184.216.34")

    def test_unknown_host_is_none(self):
        self.assertIsNone(self.net.resolve("nope.invalid"))

    def test_etc_hosts_edit_takes_effect(self):
        self.vos.write("/etc/hosts", "127.0.0.1 mserver\n10.0.2.9 db\n")
        self.assertEqual(self.net.resolve("db"), "10.0.2.9")

    def test_comments_ignored(self):
        self.vos.write("/etc/hosts", "# 10.0.2.9 ghost\n127.0.0.1 real\n")
        self.assertIsNone(self.net.resolve("ghost"))
        self.assertEqual(self.net.resolve("real"), "127.0.0.1")

    def test_missing_hosts_file_does_not_crash(self):
        self.vos.remove("/etc/hosts")
        self.assertEqual(self.net.hosts_map(), {})
        self.assertIsNone(self.net.resolve("mserver"))


class TestInterfaces(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env()

    def test_ifconfig_shows_both_interfaces(self):
        out = self.shell.run("ifconfig")[0]
        self.assertIn("lo:", out)
        self.assertIn("eth0:", out)
        self.assertIn(netmod.HOST_IP, out)

    def test_ifconfig_single_interface(self):
        out = self.shell.run("ifconfig eth0")[0]
        self.assertIn("eth0", out)
        self.assertNotIn("lo:", out)

    def test_ifconfig_unknown_interface_errors(self):
        self.assertEqual(self.shell.run("ifconfig wlan9")[2], 1)

    def test_ip_addr(self):
        self.assertIn(netmod.HOST_IP, self.shell.run("ip addr")[0])

    def test_ip_route_has_default_gateway(self):
        out = self.shell.run("ip route")[0]
        self.assertIn("default via", out)
        self.assertIn(netmod.GATEWAY, out)

    def test_ip_unknown_object_errors(self):
        self.assertEqual(self.shell.run("ip frobnicate")[2], 1)


class TestPing(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env()

    def test_ping_localhost(self):
        out, _, code = self.shell.run("ping -c 2 localhost")
        self.assertEqual(code, 0)
        self.assertIn("2 packets transmitted, 2 received", out)
        self.assertEqual(out.count("icmp_seq="), 2)

    def test_ping_count_flag(self):
        out = self.shell.run("ping -c 5 mserver")[0]
        self.assertEqual(out.count("icmp_seq="), 5)

    def test_ping_default_count(self):
        self.assertEqual(self.shell.run("ping mserver")[0].count("icmp_seq="), 4)

    def test_ping_unknown_host_fails(self):
        out, err, code = self.shell.run("ping nope.invalid")
        self.assertEqual(code, 2)
        self.assertIn("Name or service not known", err)

    def test_ping_no_args_is_usage_error(self):
        self.assertEqual(self.shell.run("ping")[2], 1)

    def test_remote_is_slower_than_loopback(self):
        net = self.vos.network
        local = float(net.ping("localhost", 1)[0].split("time=")[1].split()[0])
        remote = float(net.ping("example.com", 1)[0].split("time=")[1].split()[0])
        self.assertLess(local, remote)

    def test_latency_is_deterministic(self):
        a = self.vos.network.ping("example.com", 1)[0]
        b = self.vos.network.ping("example.com", 1)[0]
        self.assertEqual(a, b)


class TestListeners(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env()

    def test_nothing_listening_initially(self):
        self.assertEqual(self.vos.network.listeners(), [])

    def test_service_start_opens_port(self):
        self.shell.run("pkg install nginx")
        self.shell.run("service nginx start")
        self.assertIsNotNone(self.vos.network.listener_on(80))

    def test_service_stop_closes_port(self):
        self.shell.run("pkg install nginx")
        self.shell.run("service nginx start")
        self.shell.run("service nginx stop")
        self.assertIsNone(self.vos.network.listener_on(80))

    def test_netstat_lists_running_service(self):
        self.shell.run("pkg install nginx")
        self.shell.run("service nginx start")
        out = self.shell.run("netstat -tln")[0]
        self.assertIn("0.0.0.0:80", out)
        self.assertIn("nginx", out)
        self.assertIn("LISTEN", out)

    def test_ssh_listens_on_22(self):
        self.shell.run("service ssh start")
        self.assertEqual(self.vos.network.listener_on(22)["service"], "ssh")

    def test_listen_port_comes_from_config(self):
        self.shell.run("pkg install nginx")
        conf = self.vos.read("/etc/nginx/nginx.conf").replace("listen 80;", "listen 8080;")
        self.vos.write("/etc/nginx/nginx.conf", conf)
        self.shell.run("service nginx start")
        self.assertIsNotNone(self.vos.network.listener_on(8080))
        self.assertIsNone(self.vos.network.listener_on(80))


class TestHttp(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env(serve=True)

    def test_curl_serves_index(self):
        out, _, code = self.shell.run("curl http://mserver/")
        self.assertEqual(code, 0)
        self.assertIn("Hello from nginx", out)

    def test_curl_head_shows_status_line(self):
        out = self.shell.run("curl -I http://mserver/")[0]
        self.assertIn("HTTP/1.1 200 OK", out)
        self.assertIn("Server: nginx/1.25.3", out)
        self.assertNotIn("Hello from nginx", out)

    def test_curl_include_shows_headers_and_body(self):
        out = self.shell.run("curl -i http://mserver/")[0]
        self.assertIn("HTTP/1.1 200 OK", out)
        self.assertIn("Hello from nginx", out)

    def test_content_length_matches_body(self):
        resp = self.vos.network.http_get("mserver", 80, "/")
        self.assertEqual(int(resp["headers"]["Content-Length"]),
                         len(resp["body"].encode()))

    def test_404_for_missing_file(self):
        out, _, code = self.shell.run("curl http://mserver/nope.html")
        self.assertIn("404", out)
        self.assertEqual(code, 22)

    def test_403_for_directory_without_index(self):
        self.vos.write("/srv/www/sub/a.txt", "x")
        self.assertIn("403", self.shell.run("curl http://mserver/sub/")[0])

    def test_serves_nested_file(self):
        self.vos.write("/srv/www/sub/a.txt", "hello-nested")
        self.assertIn("hello-nested",
                      self.shell.run("curl http://mserver/sub/a.txt")[0])

    def test_connection_refused_when_stopped(self):
        self.shell.run("service nginx stop")
        out, err, code = self.shell.run("curl http://mserver/")
        self.assertEqual(code, 7)
        self.assertIn("Connection refused", err)

    def test_non_http_service_refuses(self):
        self.shell.run("service ssh start")
        self.assertIn("not an HTTP server",
                      self.shell.run("curl http://mserver:22/")[1])

    def test_unknown_host_fails(self):
        self.assertIn("Could not resolve",
                      self.shell.run("curl http://nope.invalid/")[1])

    def test_real_internet_is_unreachable(self):
        """The vOS network must not become a second, weaker egress path."""
        _, err, code = self.shell.run("curl http://example.com/")
        self.assertEqual(code, 7)
        self.assertIn("unreachable", err)

    def test_path_traversal_refused(self):
        out = self.shell.run("curl http://mserver/../../etc/passwd")[0]
        self.assertIn("400", out)
        self.assertNotIn("root:x:", out)

    def test_docroot_from_config(self):
        self.shell.run("service nginx stop")
        conf = self.vos.read("/etc/nginx/nginx.conf").replace(
            "root /srv/www;", "root /srv/site;")
        self.vos.write("/etc/nginx/nginx.conf", conf)
        self.vos.write("/srv/site/index.html", "custom docroot")
        self.shell.run("service nginx start")
        self.assertIn("custom docroot", self.shell.run("curl http://mserver/")[0])

    def test_content_type_by_extension(self):
        self.vos.write("/srv/www/a.css", "body{}")
        resp = self.vos.network.http_get("mserver", 80, "/a.css")
        self.assertEqual(resp["headers"]["Content-Type"], "text/css")

    def test_requests_are_logged(self):
        self.shell.run("curl http://mserver/")
        self.assertIn('"GET / HTTP/1.1" 200', self.vos.read("/var/log/nginx.log"))


class TestWget(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env(serve=True)

    def test_wget_saves_file(self):
        out, _, code = self.shell.run("wget http://mserver/index.html")
        self.assertEqual(code, 0)
        self.assertIn("saved", out)
        dest = self.shell._join(self.shell.cwd, "index.html")
        self.assertIn("Hello from nginx", self.vos.read(dest))

    def test_wget_output_name(self):
        self.shell.run("wget -O /tmp/page.html http://mserver/")
        self.assertTrue(self.vos.exists("/tmp/page.html"))

    def test_wget_404_does_not_write(self):
        _, err, code = self.shell.run("wget http://mserver/nope.html")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.vos.exists(self.shell._join(self.shell.cwd, "nope.html")))

    def test_wget_refused_when_stopped(self):
        self.shell.run("service nginx stop")
        self.assertNotEqual(self.shell.run("wget http://mserver/")[2], 0)


class TestUrlParsing(unittest.TestCase):
    def test_defaults_to_port_80(self):
        self.assertEqual(netmod.parse_url("http://mserver/"), ("mserver", 80, "/"))

    def test_https_defaults_to_443(self):
        self.assertEqual(netmod.parse_url("https://mserver/x")[1], 443)

    def test_explicit_port(self):
        self.assertEqual(netmod.parse_url("http://mserver:8080/a")[1], 8080)

    def test_bare_host_is_http(self):
        self.assertEqual(netmod.parse_url("mserver/a"), ("mserver", 80, "/a"))

    def test_no_path_becomes_root(self):
        self.assertEqual(netmod.parse_url("http://mserver")[2], "/")

    def test_bad_scheme_rejected(self):
        with self.assertRaises(netmod.NetworkError):
            netmod.parse_url("ftp://mserver/")

    def test_bad_port_rejected(self):
        with self.assertRaises(netmod.NetworkError):
            netmod.parse_url("http://mserver:notaport/")


class TestDnsCommands(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env()

    def test_host_resolves(self):
        self.assertIn("has address", self.shell.run("host mserver")[0])

    def test_host_unknown_fails(self):
        self.assertEqual(self.shell.run("host nope.invalid")[2], 1)

    def test_nslookup_reports_server(self):
        self.assertIn(netmod.DNS, self.shell.run("nslookup mserver")[0])

    def test_nslookup_nxdomain(self):
        _, err, code = self.shell.run("nslookup nope.invalid")
        self.assertEqual(code, 1)
        self.assertIn("NXDOMAIN", err)


class TestNetTool(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env(serve=True)
        _, self.ex = build_tools(self.vos, self.shell, {})

    def test_status(self):
        out = self.ex["net"]({"action": "status"})
        self.assertIn("eth0", out)
        self.assertIn("LISTEN", out)

    def test_ping(self):
        self.assertIn("icmp_seq=", self.ex["net"]({"action": "ping", "target": "mserver"}))

    def test_curl(self):
        self.assertIn("Hello from nginx",
                      self.ex["net"]({"action": "curl", "target": "http://mserver/"}))

    def test_bad_action(self):
        self.assertTrue(self.ex["net"]({"action": "explode"}).startswith("error:"))

    def test_ping_rejects_injection(self):
        out = self.ex["net"]({"action": "ping", "target": "mserver; rm -rf /"})
        self.assertTrue(out.startswith("error:"))


class TestPipelines(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env(serve=True)

    def test_curl_pipes_into_grep(self):
        out = self.shell.run("curl http://mserver/ | grep nginx")[0]
        self.assertIn("nginx", out)

    def test_netstat_pipes_into_grep(self):
        self.assertIn("80", self.shell.run("netstat -tln | grep nginx")[0])

    def test_curl_redirects_to_file(self):
        self.shell.run("curl http://mserver/ > /tmp/out.html")
        self.assertIn("Hello from nginx", self.vos.read("/tmp/out.html"))

    def test_usable_from_a_created_package(self):
        """The agent can build a package on top of the network."""
        _, ex = build_tools(self.vos, self.shell, {})
        ex["pkg_create"]({"name": "healthz", "commands": {
            "healthz": {"body": ["curl -I http://mserver/ | grep HTTP"]}}})
        self.assertIn("200", self.shell.run("healthz")[0])


class TestPersistence(unittest.TestCase):
    def test_listener_survives_reboot(self):
        vos, shell, d = new_env(serve=True)
        shell.run("reboot")
        self.assertIsNotNone(vos.network.listener_on(80))

    def test_listener_survives_restart(self):
        vos, shell, d = new_env(serve=True)
        vos2 = VOS(os.path.join(d, ".mserver"))
        Shell(vos2)
        self.assertIsNotNone(vos2.network.listener_on(80))


if __name__ == "__main__":
    unittest.main(verbosity=2)
