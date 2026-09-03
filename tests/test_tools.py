"""Agent tools tests. Run: python3 tests/test_tools.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402
from mserver.agent.tools import build_tools  # noqa: E402


def _ctx():
    tmp = tempfile.mkdtemp(prefix="mserver-tools-")
    vos = VOS(tmp)
    shell = Shell(vos)
    presented = []
    dash_msgs = []

    def on_present(title, content, fname):
        presented.append((title, content, fname))

    def dash(action, port):
        dash_msgs.append((action, port))
        return f"dash:{action}:{port}"

    schemas, ex = build_tools(vos, shell, {"on_present": on_present, "dashboard": dash})
    return vos, shell, ex, presented, dash_msgs, schemas


def test_schemas_wellformed():
    *_, schemas = _ctx()
    names = [s["function"]["name"] for s in schemas]
    assert "vos_run" in names and "present" in names and "dashboard" in names
    for s in schemas:
        assert s["type"] == "function"
        assert s["function"]["parameters"]["type"] == "object"
        assert set(s["function"]["parameters"]["required"]) <= set(
            s["function"]["parameters"]["properties"]
        )


def test_write_read_run():
    vos, shell, ex, *_ = _ctx()
    assert "bytes" in ex["vos_write"]({"path": "/root/a.txt", "content": "hello"})
    assert ex["vos_read"]({"path": "/root/a.txt"}) == "hello"
    out = ex["vos_run"]({"command": "cat /root/a.txt"})
    assert "hello" in out
    out = ex["vos_run"]({"command": "definitely-not-a-cmd"})
    assert "exit 127" in out
    assert "no such file" in ex["vos_read"]({"path": "/nope.txt"})


def test_present_and_sandbox():
    vos, shell, ex, presented, *_ = _ctx()
    res = ex["present"]({"title": "My Report", "content": "line1\nline2"})
    assert "presented" in res
    assert presented and presented[0][0] == "My Report"

    # path traversal must fail and must not touch the real /etc
    res = ex["vos_write"]({"path": "/../../etc/passwd", "content": "x"})
    assert "error" in res
    assert "root:x:0:0" in vos.read("/etc/passwd")  # base file intact

    res = ex["vos_delete"]({"path": "/etc/passwd"})
    assert "removed" in res
    assert not vos.exists("/etc/passwd")  # deleted *inside* the sandbox


def test_pkg_tools():
    vos, shell, ex, *_ = _ctx()
    res = ex["pkg_install"]({"name": "nginx"})
    assert "OK" in res
    out = ex["vos_run"]({"command": "nginx -v"})
    assert "nginx/1.25.3" in out
    assert "nginx" in vos.installed_packages()
    res = ex["pkg_install"]({"name": "bad;rm -rf /"})
    assert "error" in res


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
