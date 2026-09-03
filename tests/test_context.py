"""Context compaction, audit log and loop guard tests.

Run: python3 tests/test_context.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mserver.agent import context  # noqa: E402
from mserver.agent.audit import AuditLog, LoopGuard  # noqa: E402
from mserver.agent.core import Agent  # noqa: E402
from mserver.agent.ui import UI  # noqa: E402
from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402


def _call(cid, name, args_raw):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": args_raw}}]}


def _result(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _convo(n=12, payload=None):
    """System prompt + n (assistant call, tool result) pairs."""
    payload = payload or ("X" * 2000)
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append(_call(f"c{i}", "vos_read", f'{{"path": "/f{i}"}}'))
        msgs.append(_result(f"c{i}", payload))
    return msgs


# ------------------------------------------------------------------ estimate
def test_estimate_and_trigger():
    small = [{"role": "system", "content": "hi"}]
    assert context.estimate_tokens(small) < 10
    msgs, report = context.compact(small, max_tokens=8000)
    assert report["compacted"] is False
    assert msgs is small


# --------------------------------------------------------------------- masking
def test_masking_shrinks_and_keeps_recent():
    msgs = _convo(12)
    before = context.estimate_tokens(msgs)
    out, report = context.compact(msgs, max_tokens=2000)
    after = context.estimate_tokens(out)
    assert report["compacted"] is True
    assert after < before, f"{after} !< {before}"
    assert report["masked"] > 0

    # the most recent tool results stay verbatim
    tools = [m for m in out if m["role"] == "tool"]
    assert not tools[-1]["content"].startswith("[output:")
    assert tools[0]["content"].startswith("[output:")


def test_short_outputs_never_masked():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(30):
        msgs.append(_call(f"c{i}", "vos_run", f'{{"command": "ls {i}"}}'))
        msgs.append(_result(f"c{i}", "ok"))
    out, _ = context.compact(msgs, max_tokens=1000)
    assert all(m["content"] == "ok" for m in out if m["role"] == "tool")


# ------------------------------------------------------------------- invariant
def test_tool_call_pairing_preserved():
    """The endpoint rejects a history where tool results lose their pairing."""
    msgs = _convo(20)
    out, _ = context.compact(msgs, max_tokens=1500)

    assert len(out) == len(msgs), "compaction must not drop messages"
    assert out[0]["role"] == "system" and out[0]["content"] == "sys"

    ids = {tc["id"] for m in out if m.get("tool_calls") for tc in m["tool_calls"]}
    for m in out:
        if m["role"] == "tool":
            assert m["tool_call_id"] in ids
    # order preserved
    assert [m["role"] for m in out] == [m["role"] for m in msgs]


def test_system_prompt_never_touched():
    """Prompt-prefix stability matters for provider-side prompt caching."""
    msgs = _convo(20)
    original = msgs[0]["content"]
    out, _ = context.compact(msgs, max_tokens=500)
    assert out[0]["content"] == original


# --------------------------------------------------------------------- dedupe
def test_dedupe_keeps_latest():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(4):
        msgs.append(_call(f"c{i}", "vos_read", '{"path": "/same"}'))
        msgs.append(_result(f"c{i}", "SAME-CONTENT " + "y" * 200))
    out, n = context.dedupe_tool_results(msgs)
    assert n == 3
    tools = [m for m in out if m["role"] == "tool"]
    assert tools[-1]["content"].startswith("SAME-CONTENT")
    assert all("duplicate of a later" in m["content"] for m in tools[:-1])


# ------------------------------------------------------------- error purging
def test_resolved_errors_purged():
    msgs = [
        {"role": "system", "content": "sys"},
        _call("c1", "vos_run", '{"command": "pkg install nginx"}'),
        _result("c1", "error: transient failure " + "z" * 100),
        _call("c2", "vos_run", '{"command": "pkg install nginx"}'),
        _result("c2", "Installing nginx ... OK"),
    ]
    out, n = context.purge_resolved_errors(msgs)
    assert n == 1
    assert "resolved by a later call" in out[2]["content"]
    assert out[4]["content"] == "Installing nginx ... OK"


def test_unresolved_error_kept():
    msgs = [
        {"role": "system", "content": "sys"},
        _call("c1", "vos_run", '{"command": "boom"}'),
        _result("c1", "error: still broken"),
    ]
    out, n = context.purge_resolved_errors(msgs)
    assert n == 0
    assert out[2]["content"] == "error: still broken"


# ------------------------------------------------------------------ idempotent
def test_compaction_is_stable():
    """Repeated compaction must converge, not chew the history each pass."""
    msgs = _convo(15)
    once, _ = context.compact(msgs, max_tokens=1500)
    twice, report = context.compact(list(once), max_tokens=1500)
    assert context.estimate_tokens(twice) <= context.estimate_tokens(once)
    assert len(twice) == len(once)


# ------------------------------------------------------------------ loop guard
def test_loop_guard_fires_on_repeat():
    g = LoopGuard(window=20, threshold=3)
    assert g.check("vos_run", {"command": "ls"}) is None
    assert g.check("vos_run", {"command": "ls"}) is None
    warn = g.check("vos_run", {"command": "ls"})
    assert warn and "3 times" in warn


def test_loop_guard_ignores_varied_calls():
    g = LoopGuard()
    for i in range(10):
        assert g.check("vos_run", {"command": f"ls /dir{i}"}) is None


# ----------------------------------------------------------------- audit log
def _vos():
    return VOS(Path(tempfile.mkdtemp(prefix="mserver-audit-")) / "vos")


def test_audit_writes_to_var_log():
    vos = _vos()
    log = AuditLog(vos)
    log.log("vos_write", {"path": "/root/a"}, "12 bytes written", ok=True)
    log.log("vos_delete", {"path": "/nope"}, "error: no such file", ok=False)
    text = vos.read("/var/log/agent.log")
    assert "vos_write" in text and "ok" in text
    assert "vos_delete" in text and "ERR" in text
    assert "path=/root/a" in text


def test_audit_is_readable_from_the_shell():
    vos = _vos()
    shell = Shell(vos)
    AuditLog(vos).log("present", {"title": "Report"}, "presented")
    out, err, code = shell.run("cat /var/log/agent.log")
    assert code == 0 and "present" in out, (out, err)
    out, _, _ = shell.run("grep present /var/log/agent.log")
    assert "present" in out


def test_audit_never_raises():
    class Broken:
        def vpath(self, p):
            raise RuntimeError("disk on fire")

    AuditLog(Broken()).log("vos_run", {"command": "ls"}, "out")
    AuditLog(Broken()).note("hello")  # must not raise


def test_audit_clips_huge_values():
    vos = _vos()
    AuditLog(vos).log("vos_write", {"content": "A" * 5000}, "B" * 5000)
    line = vos.read("/var/log/agent.log").splitlines()[0]
    assert len(line) < 400
    assert "…" in line


# ------------------------------------------------------- agent integration
def _agent():
    tmp = Path(tempfile.mkdtemp(prefix="mserver-agent-"))
    vos = VOS(tmp / "vos")
    return Agent(vos, Shell(vos), UI(), tmp / "presented", force_local=True), vos


def test_agent_logs_offline_commands():
    agent, vos = _agent()
    agent.ask("neofetch")
    text = vos.read("/var/log/agent.log")
    assert "session start" in text
    assert "offline" in text and "neofetch" in text


def test_agent_compacts_oversized_history():
    agent, vos = _agent()
    agent.max_context_tokens = 1200
    agent.messages = _convo(14)
    before = agent.context_tokens()
    agent._compact()
    assert agent.context_tokens() < before
    assert "context compacted" in vos.read("/var/log/agent.log")


def test_agent_reset():
    agent, _ = _agent()
    agent.messages.append({"role": "user", "content": "hi"})
    msg = agent.reset()
    assert "reset" in msg
    assert len(agent.messages) == 1 and agent.messages[0]["role"] == "system"


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
