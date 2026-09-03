"""Snapshots, risk tiers, the confirmation gate and vos_edit.

Run: python3 tests/test_safety.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mserver.agent import llm, risk  # noqa: E402
from mserver.agent.core import Agent  # noqa: E402
from mserver.agent.tools import build_tools  # noqa: E402
from mserver.agent.ui import UI  # noqa: E402
from mserver.vos import snapshots as snapmod  # noqa: E402
from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402


def _shell():
    base = Path(tempfile.mkdtemp(prefix="mserver-safety-"))
    vos = VOS(base / "vos")
    return vos, Shell(vos)


# ---------------------------------------------------------------- snapshots
def test_snapshot_save_and_list():
    vos, sh = _shell()
    vos.write("/root/a.txt", "hello")
    meta = sh.snapshots.save("s1", label="first")
    assert meta["name"] == "s1" and meta["files"] > 0
    snaps = sh.snapshots.list()
    assert len(snaps) == 1 and snaps[0]["label"] == "first"


def test_snapshot_restores_deleted_tree():
    """The whole point: rm -rf must be survivable."""
    vos, sh = _shell()
    vos.write("/root/important.txt", "precious")
    sh.snapshots.save("before")
    sh.run("rm -rf /root")
    assert not vos.exists("/root/important.txt")
    sh.snapshots.rollback("before")
    assert vos.read("/root/important.txt") == "precious"


def test_rollback_is_itself_undoable():
    vos, sh = _shell()
    vos.write("/root/v1.txt", "one")
    sh.snapshots.save("v1")
    vos.remove("/root/v1.txt")
    vos.write("/root/v2.txt", "two")
    meta = sh.snapshots.rollback("v1")
    assert vos.exists("/root/v1.txt") and not vos.exists("/root/v2.txt")
    # rolling back the rollback brings v2 back
    sh.snapshots.rollback(meta["undo"])
    assert vos.exists("/root/v2.txt")


def test_snapshots_live_outside_the_rootfs():
    """An undo history the agent can delete is not an undo history."""
    vos, sh = _shell()
    sh.snapshots.save("safe")
    assert sh.snapshots.base.resolve() != vos.root.resolve()
    assert not str(sh.snapshots.base.resolve()).startswith(str(vos.root.resolve()) + "/")
    # and it is not reachable through the shell
    out, err, code = sh.run("ls /snapshots")
    assert code != 0 or "safe" not in out
    sh.run("rm -rf /")
    assert sh.snapshots.exists("safe")


def test_snapshot_bad_names_rejected():
    vos, sh = _shell()
    for bad in ["../escape", "a/b", "", "x" * 80, "-leading"]:
        try:
            sh.snapshots.save(bad)
            raise AssertionError(f"accepted bad name {bad!r}")
        except snapmod.SnapshotError:
            pass


def test_snapshot_duplicate_rejected():
    vos, sh = _shell()
    sh.snapshots.save("dup")
    try:
        sh.snapshots.save("dup")
        raise AssertionError("duplicate accepted")
    except snapmod.SnapshotError:
        pass


def test_snapshot_shell_command():
    vos, sh = _shell()
    out, _, code = sh.run("snapshot")
    assert code == 0 and "no snapshots" in out
    vos.write("/root/x", "1")
    out, err, code = sh.run("snapshot save mysnap the label")
    assert code == 0 and "mysnap" in out, (out, err)
    out, _, _ = sh.run("snapshot list")
    assert "mysnap" in out and "the label" in out
    sh.run("rm -rf /root")
    out, err, code = sh.run("snapshot rollback mysnap")
    assert code == 0, err
    assert vos.exists("/root/x")
    out, _, code = sh.run("snapshot rm mysnap")
    assert code == 0
    out, err, code = sh.run("snapshot rollback nope")
    assert code == 1 and "no such snapshot" in err


def test_rollback_resets_dangling_cwd():
    vos, sh = _shell()
    sh.snapshots.save("base")
    sh.run("mkdir /tmp/deep")
    sh.run("cd /tmp/deep")
    assert sh.cwd == "/tmp/deep"
    sh.run("snapshot rollback base")
    assert sh.cwd == "/"


# --------------------------------------------------------------- risk tiers
def test_tier_classification():
    assert risk.classify("vos_read", {"path": "/x"}) == risk.READ
    assert risk.classify("vos_write", {"path": "/x"}) == risk.WRITE
    assert risk.classify("vos_edit", {"path": "/x"}) == risk.WRITE
    assert risk.classify("vos_delete", {"path": "/x"}) == risk.DESTRUCTIVE
    assert risk.classify("snapshot_rollback", {"name": "s"}) == risk.DESTRUCTIVE
    assert risk.classify("vos_run", {"command": "ls -la /"}) == risk.WRITE
    assert risk.classify("vos_run", {"command": "rm -rf /etc"}) == risk.DESTRUCTIVE


def test_destructive_hidden_in_a_pipeline_is_caught():
    """The model must not smuggle rm past the gate behind a pipe or ;."""
    for cmd in ["cat /etc/hosts | rm -rf /root",
                "echo hi; rm -rf /etc",
                "ls && rm -f /root/a",
                "snapshot rm important"]:
        assert risk.classify("vos_run", {"command": cmd}) == risk.DESTRUCTIVE, cmd


def test_root_wipe_detection():
    assert risk.is_root_wipe("vos_run", {"command": "rm -rf /"})
    assert risk.is_root_wipe("vos_run", {"command": "rm -rf /etc"})
    assert risk.is_root_wipe("vos_delete", {"path": "/"})
    assert risk.is_root_wipe("vos_delete", {"path": "/var"})
    assert not risk.is_root_wipe("vos_run", {"command": "rm /tmp/scratch.txt"})
    assert not risk.is_root_wipe("vos_delete", {"path": "/root/notes.md"})


# --------------------------------------------------------------------- gate
def test_gate_allows_reads_and_writes():
    g = risk.Gate(mode="deny")
    assert g.check("vos_read", {"path": "/x"})[0] is True
    assert g.check("vos_write", {"path": "/x", "content": "y"})[0] is True
    assert g.check("vos_run", {"command": "ls"})[0] is True


def test_gate_deny_mode():
    g = risk.Gate(mode="deny")
    ok, reason = g.check("vos_delete", {"path": "/etc"})
    assert ok is False and "disabled" in reason
    assert g.blocked == 1


def test_gate_allow_mode():
    g = risk.Gate(mode="allow")
    assert g.check("vos_delete", {"path": "/etc"})[0] is True
    assert g.approved == 1


def test_gate_ask_mode_yes_and_no():
    yes = risk.Gate(mode="ask", confirm=lambda p: True)
    assert yes.check("vos_delete", {"path": "/x"})[0] is True

    no = risk.Gate(mode="ask", confirm=lambda p: False)
    ok, reason = no.check("vos_delete", {"path": "/x"})
    assert ok is False and "refused by the user" in reason


def test_gate_fails_closed_without_a_prompt():
    """A headless session (web chat) must refuse, not silently allow."""
    g = risk.Gate(mode="ask", confirm=None)
    ok, reason = g.check("vos_delete", {"path": "/etc"})
    assert ok is False and "cannot prompt" in reason


def test_gate_confirm_prompt_warns_on_root_wipe():
    seen = {}

    def confirm(prompt):
        seen["prompt"] = prompt
        return False

    risk.Gate(mode="ask", confirm=confirm).check("vos_run", {"command": "rm -rf /"})
    assert "ERASE" in seen["prompt"]


def test_gate_autosnapshots_before_approved_wipe():
    taken = []
    g = risk.Gate(mode="allow", on_snapshot=lambda label: taken.append(label))
    g.check("vos_run", {"command": "rm -rf /etc"})
    assert len(taken) == 1
    g.check("vos_delete", {"path": "/root/one-file.txt"})
    assert len(taken) == 1, "small deletes should not snapshot"


# ----------------------------------------------------------------- vos_edit
def _tools():
    vos, sh = _shell()
    schemas, ex = build_tools(vos, sh, {"on_present": lambda *a: None,
                                        "dashboard": lambda *a: ""})
    return vos, sh, ex, schemas


def test_vos_edit_replaces_fragment():
    vos, sh, ex, _ = _tools()
    vos.write("/etc/app.conf", "host = localhost\nport = 80\n")
    out = ex["vos_edit"]({"path": "/etc/app.conf", "old": "port = 80",
                          "new": "port = 8080"})
    assert "edited" in out
    assert vos.read("/etc/app.conf") == "host = localhost\nport = 8080\n"


def test_vos_edit_requires_unique_match():
    vos, sh, ex, _ = _tools()
    vos.write("/f.txt", "a\na\n")
    out = ex["vos_edit"]({"path": "/f.txt", "old": "a", "new": "b"})
    assert "2 matches" in out
    assert vos.read("/f.txt") == "a\na\n", "file must be untouched on failure"


def test_vos_edit_missing_text_and_file():
    vos, sh, ex, _ = _tools()
    vos.write("/f.txt", "hello")
    assert "not found" in ex["vos_edit"]({"path": "/f.txt", "old": "zzz", "new": "x"})
    assert "error" in ex["vos_edit"]({"path": "/nope", "old": "a", "new": "b"})
    assert "must not be empty" in ex["vos_edit"]({"path": "/f.txt", "old": "", "new": "x"})


def test_vos_edit_cannot_escape_sandbox():
    vos, sh, ex, _ = _tools()
    out = ex["vos_edit"]({"path": "../../../../etc/passwd", "old": "root", "new": "pwn"})
    assert "error" in out and "escapes" in out


def test_snapshot_tools_registered():
    vos, sh, ex, schemas = _tools()
    names = [s["function"]["name"] for s in schemas]
    for n in ("snapshot_save", "snapshot_rollback", "snapshot_list", "vos_edit"):
        assert n in names and n in ex

    assert "no snapshots" in ex["snapshot_list"]({})
    assert "saved snapshot" in ex["snapshot_save"]({"name": "t1"})
    assert "t1" in ex["snapshot_list"]({})
    vos.write("/root/gone.txt", "x")
    assert "rolled back" in ex["snapshot_rollback"]({"name": "t1"})
    assert not vos.exists("/root/gone.txt")


# ------------------------------------------------------- agent integration
def _agent(mode, confirm=None):
    base = Path(tempfile.mkdtemp(prefix="mserver-agent-safety-"))
    vos = VOS(base / "vos")
    sh = Shell(vos)
    a = Agent(vos, sh, UI(), base / "presented", force_local=True,
              gate_mode=mode, confirm=confirm)
    a.local = False
    a.cfg.model = "fake"
    return a, vos, sh


def _one_shot(tool, args):
    """An LLM that calls one tool then stops."""
    state = {"n": 0}

    def fake(messages, tools, cfg):
        state["n"] += 1
        if state["n"] == 1:
            return {"content": None, "tool_calls": [
                {"id": "c1", "name": tool, "arguments_raw": json.dumps(args)}]}
        return {"content": "finished", "tool_calls": []}

    return fake


def test_agent_gate_blocks_destructive_call():
    agent, vos, _ = _agent("deny")
    vos.write("/etc/keep.txt", "precious")
    llm.chat = _one_shot("vos_run", {"command": "rm -rf /etc"})
    agent.ask("wipe etc")
    assert vos.exists("/etc/keep.txt"), "gate did not stop the deletion"
    assert agent.gate.blocked == 1
    assert "refused" in vos.read("/var/log/agent.log")


def test_agent_gate_allows_when_confirmed():
    agent, vos, sh = _agent("ask", confirm=lambda p: True)
    vos.write("/etc/keep.txt", "precious")
    llm.chat = _one_shot("vos_run", {"command": "rm -rf /etc"})
    agent.ask("wipe etc")
    assert not vos.exists("/etc/keep.txt")
    # a snapshot was taken automatically first, so it is recoverable
    assert sh.snapshots.list(), "no auto-snapshot before a root wipe"
    sh.snapshots.rollback(sh.snapshots.list()[0]["name"])
    assert vos.exists("/etc/keep.txt"), "auto-snapshot did not capture the file"


def test_agent_gate_refusal_is_logged_and_fed_back():
    agent, vos, _ = _agent("deny")
    llm.chat = _one_shot("vos_delete", {"path": "/etc"})
    agent.ask("delete etc")
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert tool_msgs and "refused" in tool_msgs[-1]["content"]


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
