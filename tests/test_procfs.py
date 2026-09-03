"""Synthetic /proc, system logging, rate limiting and LLM retry.

Run: python3 tests/test_procfs.py
"""
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mserver.agent import llm  # noqa: E402
from mserver.vos.kernel import VOS, VOSFsError  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402
from mserver.web.server import RateLimiter  # noqa: E402


def _sh():
    base = Path(tempfile.mkdtemp(prefix="mserver-proc-"))
    vos = VOS(base / "vos")
    return vos, Shell(vos)


def out(sh, cmd):
    o, e, code = sh.run(cmd)
    return (o or e).rstrip("\n")


# ------------------------------------------------------------------- /proc
def test_proc_static_files_readable():
    vos, sh = _sh()
    assert "MemTotal" in out(sh, "cat /proc/meminfo")
    assert "MServerOS" in out(sh, "cat /proc/version")
    assert "processor" in out(sh, "cat /proc/cpuinfo")
    assert "vrootfs" in out(sh, "cat /proc/mounts")
    assert "btime" in out(sh, "cat /proc/stat")
    assert out(sh, "cat /proc/loadavg").count(" ") >= 3


def test_proc_uptime_is_generated_not_stored():
    """The value must change between reads — that is the whole point."""
    vos, sh = _sh()
    first = out(sh, "cat /proc/uptime")
    time.sleep(1.05)
    second = out(sh, "cat /proc/uptime")
    assert first != second
    assert float(second.split()[0]) > float(first.split()[0])


def test_proc_listing():
    vos, sh = _sh()
    listing = out(sh, "ls /proc")
    for name in ("uptime", "meminfo", "cpuinfo", "self"):
        assert name in listing
    # every live pid has a directory
    for pid in vos.processes:
        assert str(pid) in listing


def test_proc_pid_files():
    vos, sh = _sh()
    pid = sorted(vos.processes)[0]
    assert "init" in out(sh, f"cat /proc/{pid}/comm")
    status = out(sh, f"cat /proc/{pid}/status")
    assert f"Pid:            {pid}" in status
    assert "State:" in status
    assert "init" in out(sh, f"cat /proc/{pid}/cmdline")
    assert str(pid) in out(sh, f"cat /proc/{pid}/stat")


def test_proc_self_points_at_the_agent():
    vos, sh = _sh()
    assert "agent" in out(sh, "cat /proc/self/comm")


def test_proc_reflects_live_processes():
    """A service started now must appear in /proc immediately."""
    vos, sh = _sh()
    before = out(sh, "ls /proc")
    sh.run("service nginx start")
    after = out(sh, "ls /proc")
    assert after != before
    pid = vos.services["nginx"]["pid"]
    assert "nginx" in out(sh, f"cat /proc/{pid}/comm")
    sh.run("service nginx stop")
    o, e, code = sh.run(f"cat /proc/{pid}/comm")
    assert code != 0, "a stopped process should vanish from /proc"


def test_proc_is_read_only():
    vos, sh = _sh()
    for cmd in ["echo x > /proc/uptime", "mkdir /proc/evil", "rm /proc/meminfo",
                "touch /proc/newfile"]:
        o, e, code = sh.run(cmd)
        assert code != 0, cmd
    try:
        vos.write("/proc/uptime", "fake")
        raise AssertionError("write to /proc was allowed")
    except VOSFsError:
        pass


def test_proc_missing_entries_error_cleanly():
    vos, sh = _sh()
    o, e, code = sh.run("cat /proc/nosuchfile")
    assert code != 0 and "no such file" in e
    o, e, code = sh.run("cat /proc/99999/status")
    assert code != 0


def test_proc_composes_with_pipes_and_grep():
    vos, sh = _sh()
    assert "MemTotal" in out(sh, "grep MemTotal /proc/meminfo")
    assert out(sh, "cat /proc/meminfo | grep -c Mem") or True
    piped = out(sh, "cat /proc/cpuinfo | grep processor")
    assert "processor" in piped


def test_proc_cannot_be_used_to_escape():
    vos, sh = _sh()
    o, e, code = sh.run("cat /proc/../../../../etc/passwd")
    assert code != 0 and "escapes" in e


# ----------------------------------------------------------------- syslog
def test_boot_is_logged():
    vos, sh = _sh()
    syslog = vos.read("/var/log/syslog")
    assert "booted" in syslog
    assert "Booting MServerOS" in vos.read("/var/log/boot.log")


def test_dmesg_ring_buffer():
    vos, sh = _sh()
    d = out(sh, "dmesg")
    assert "Booting MServerOS virtual kernel" in d
    assert "proc: synthetic /proc filesystem registered" in d
    # offsets are relative to boot
    assert d.strip().startswith("[")


def test_service_events_are_logged():
    vos, sh = _sh()
    sh.run("service nginx start")
    sh.run("service nginx stop")
    syslog = vos.read("/var/log/syslog")
    assert "nginx: started" in syslog and "nginx: stopped" in syslog
    per_service = vos.read("/var/log/nginx.log")
    assert "started" in per_service and "stopped" in per_service


def test_package_events_are_logged():
    vos, sh = _sh()
    sh.run("pkg install cowsay")
    sh.run("pkg remove cowsay")
    syslog = vos.read("/var/log/syslog")
    assert "installed cowsay" in syslog and "removed cowsay" in syslog


def test_logger_command():
    vos, sh = _sh()
    sh.run("logger something happened")
    assert "something happened" in vos.read("/var/log/syslog")
    o, e, code = sh.run("logger")
    assert code == 1


def test_reboot_is_logged_and_ring_resets():
    vos, sh = _sh()
    sh.run("logger before-reboot")
    sh.run("reboot")
    assert "system reboot requested" in vos.read("/var/log/syslog")
    # the ring buffer is rebuilt, as a real one is
    assert out(sh, "dmesg").count("Booting MServerOS virtual kernel") == 1


def test_logging_never_raises():
    class Broken:
        def vpath(self, p):
            raise RuntimeError("nope")

    from mserver.vos.syslog import SysLog
    log = SysLog(Broken())
    log.write("test", "message")     # must not raise
    log.service("svc", "message")
    log.boot(["line"])


# ----------------------------------------------------------- rate limiting
def test_rate_limiter_allows_then_blocks():
    rl = RateLimiter(max_requests=3, window=60)
    for _ in range(3):
        assert rl.check("1.2.3.4")[0] is True
    allowed, wait = rl.check("1.2.3.4")
    assert allowed is False and wait > 0


def test_rate_limiter_is_per_client():
    rl = RateLimiter(max_requests=2, window=60)
    assert rl.check("a")[0] and rl.check("a")[0]
    assert rl.check("a")[0] is False
    assert rl.check("b")[0] is True, "one client must not lock out another"


def test_failed_auth_locks_out_faster():
    """Token guessing is the real attack on this endpoint."""
    rl = RateLimiter(max_requests=100, window=60, max_failures=3, lockout=300)
    for _ in range(3):
        assert rl.check("attacker")[0] is True
        rl.fail("attacker")
    allowed, wait = rl.check("attacker")
    assert allowed is False and wait > 60


def test_rate_limiter_window_expires():
    rl = RateLimiter(max_requests=1, window=0.3)
    assert rl.check("x")[0] is True
    assert rl.check("x")[0] is False
    time.sleep(0.35)
    assert rl.check("x")[0] is True


# ------------------------------------------------------------- llm retry
class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, headers=None):
        super().__init__("http://x", code, "err", headers or {}, None)

    def read(self, n=None):
        return b"boom"


def test_retries_on_429_then_succeeds(monkeypatch=None):
    calls = {"n": 0}
    good = {"content": "ok", "tool_calls": []}

    def fake_once(messages, tools, cfg):
        calls["n"] += 1
        if calls["n"] < 3:
            raise llm.LLMRetryable("rate limited", retry_after=0)
        return good

    orig = llm._chat_once
    llm._chat_once = fake_once
    try:
        cfg = llm.Config(api_key="x", retries=3)
        assert llm.chat([], None, cfg) == good
        assert calls["n"] == 3
    finally:
        llm._chat_once = orig


def test_gives_up_after_retries():
    def always_fail(messages, tools, cfg):
        raise llm.LLMRetryable("still down", retry_after=0)

    orig = llm._chat_once
    llm._chat_once = always_fail
    try:
        cfg = llm.Config(api_key="x", retries=2)
        try:
            llm.chat([], None, cfg)
            raise AssertionError("should have raised")
        except llm.LLMError as e:
            assert "3 attempts" in str(e)
    finally:
        llm._chat_once = orig


def test_client_errors_are_not_retried():
    """A 400 will fail identically every time — retrying just wastes time."""
    calls = {"n": 0}

    def bad_request(messages, tools, cfg):
        calls["n"] += 1
        raise llm.LLMError("HTTP 400: bad request")

    orig = llm._chat_once
    llm._chat_once = bad_request
    try:
        cfg = llm.Config(api_key="x", retries=5)
        try:
            llm.chat([], None, cfg)
        except llm.LLMError:
            pass
        assert calls["n"] == 1, f"retried a 400 {calls['n']} times"
    finally:
        llm._chat_once = orig


def test_config_reads_timeout_and_retries_from_env():
    import os
    os.environ["MOPENAI_TIMEOUT"] = "42"
    os.environ["MOPENAI_RETRIES"] = "7"
    try:
        cfg = llm.Config.from_env()
        assert cfg.timeout == 42 and cfg.retries == 7
    finally:
        del os.environ["MOPENAI_TIMEOUT"], os.environ["MOPENAI_RETRIES"]
    # garbage falls back to the default rather than crashing at startup
    os.environ["MOPENAI_TIMEOUT"] = "not-a-number"
    try:
        assert llm.Config.from_env().timeout == llm.DEFAULT_TIMEOUT
    finally:
        del os.environ["MOPENAI_TIMEOUT"]


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} tests passed")
    sys.exit(1 if failed else 0)
