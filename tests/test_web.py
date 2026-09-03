"""Web dashboard + chat tests. Run: python3 tests/test_web.py"""
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mserver.agent.core import Agent  # noqa: E402
from mserver.agent.ui import UI  # noqa: E402
from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402
from mserver.web.server import Dashboard  # noqa: E402

TOKEN = "test-token-123"


def _start():
    tmp = Path(tempfile.mkdtemp(prefix="mserver-web-"))
    vos = VOS(tmp / "vos")
    shell = Shell(vos)
    agent = Agent(vos, shell, UI(), tmp / "presented", force_local=True)
    dash = Dashboard(vos, shell, tmp / "presented", port=0, host="127.0.0.1",
                     agent=agent, token=TOKEN)
    dash.start()
    return dash


def _get(dash, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{dash.actual_port}{path}", timeout=15) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _post(dash, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{dash.actual_port}/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_status_page():
    dash = _start()
    code, body = _get(dash, "/")
    assert code == 200
    assert "MServerOS" in body and "talk to the agent" in body
    dash.stop()


def test_chat_page_served_without_token():
    dash = _start()
    code, body = _get(dash, "/chat")
    assert code == 200
    assert "token" in body
    dash.stop()


def test_chat_requires_token():
    dash = _start()
    code, body = _post(dash, {"token": "wrong", "message": "hi"})
    assert code == 401 and body["ok"] is False
    code, body = _post(dash, {"message": "no token at all"})
    assert code == 401
    dash.stop()


def test_chat_roundtrip_offline():
    dash = _start()
    code, body = _post(dash, {"token": TOKEN, "message": "neofetch"})
    assert code == 200 and body["ok"] is True
    assert "MServerOS" in body["reply"]
    assert any(e["name"] == "neofetch" for e in body["events"])

    code, body = _post(dash, {"token": TOKEN, "message": "install cowsay"})
    assert code == 200 and "OK" in body["reply"]
    code, body = _post(dash, {"token": TOKEN, "message": "cowsay hello from web"})
    assert code == 200 and "< hello from web >" in body["reply"]
    dash.stop()


def test_chat_rejects_empty_and_bad_json():
    dash = _start()
    code, body = _post(dash, {"token": TOKEN, "message": "   "})
    assert code == 400
    # raw malformed JSON body
    req = urllib.request.Request(
        f"http://127.0.0.1:{dash.actual_port}/chat",
        data=b"{not json", headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected HTTP error")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    dash.stop()


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
