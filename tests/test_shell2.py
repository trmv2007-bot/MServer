"""Shell correctness: variables, aliases, chaining, history, coreutils, man.

Covers the shell bugs found during the audit:
  - `echo $HOME` printed the literal "$HOME" (no expansion at all)
  - /root/.mshrc was created at boot and never read; `alias` did not exist
  - history was memory-only and lost on exit
  - services were wiped by `reboot`

Run: python3 tests/test_shell2.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402


def _sh(base=None):
    base = base or Path(tempfile.mkdtemp(prefix="mserver-sh2-"))
    return Shell(VOS(base / "vos")), base


def out(sh, cmd):
    o, e, code = sh.run(cmd)
    return (o or e).rstrip("\n")


# --------------------------------------------------------- variable expansion
def test_expands_plain_and_braced():
    sh, _ = _sh()
    assert out(sh, "echo $HOME") == "/root"
    assert out(sh, "echo ${HOME}/docs") == "/root/docs"
    assert out(sh, "echo $USER") == "root"


def test_single_quotes_are_literal():
    """The whole point of single quotes."""
    sh, _ = _sh()
    assert out(sh, "echo '$HOME'") == "$HOME"
    assert out(sh, 'echo "$HOME"') == "/root"


def test_undefined_expands_to_empty():
    sh, _ = _sh()
    assert out(sh, "echo [$NOPE_NOT_SET]") == "[]"


def test_exit_status_variable():
    sh, _ = _sh()
    sh.run("echo hi")
    assert out(sh, "echo $?") == "0"
    sh.run("definitely-not-a-command")
    assert out(sh, "echo $?") == "127"


def test_export_and_unset():
    sh, _ = _sh()
    sh.run("export GREETING=hello")
    assert out(sh, "echo $GREETING world") == "hello world"
    assert "GREETING=hello" in out(sh, "env")
    sh.run("unset GREETING")
    assert out(sh, "echo [$GREETING]") == "[]"


def test_export_rejects_bad_names():
    sh, _ = _sh()
    o, e, code = sh.run("export 9bad=x")
    assert code == 1 and "invalid" in e


def test_pwd_variable_tracks_cd():
    sh, _ = _sh()
    sh.run("mkdir /tmp/sub")
    sh.run("cd /tmp/sub")
    assert out(sh, "echo $PWD") == "/tmp/sub"


# ------------------------------------------------------------------- aliases
def test_mshrc_is_loaded_at_boot():
    """/root/.mshrc shipped with `alias ll='ls -la'` and was never read."""
    sh, _ = _sh()
    assert sh.aliases.get("ll") == "ls -la"
    listing = out(sh, "ll")
    assert "etc" in listing and "root" in listing


def test_alias_define_list_remove():
    sh, _ = _sh()
    sh.run("alias hi='echo hello'")
    assert out(sh, "hi") == "hello"
    assert "hi" in out(sh, "alias")
    sh.run("unalias hi")
    o, e, code = sh.run("hi")
    assert code == 127


def test_alias_takes_extra_arguments():
    sh, _ = _sh()
    sh.run("alias e='echo'")
    assert out(sh, "e one two") == "one two"


def test_alias_loop_is_survivable():
    """A self-referential alias must not hang the shell."""
    sh, _ = _sh()
    sh.run("alias ls='ls -la'")
    assert "etc" in out(sh, "ls")
    sh.run("alias a='b'")
    sh.run("alias b='a'")
    o, e, code = sh.run("a")
    assert code != 0 or o == ""


def test_source_runs_a_script():
    sh, _ = _sh()
    sh.vos.write("/root/setup.msh", "# comment\nexport FROM_FILE=yes\nalias z='echo zed'\n")
    sh.run("source /root/setup.msh")
    assert out(sh, "echo $FROM_FILE") == "yes"
    assert out(sh, "z") == "zed"


# ------------------------------------------------------------------ chaining
def test_and_or_chaining():
    sh, _ = _sh()
    assert out(sh, "echo a && echo b") == "b"
    assert out(sh, "nosuchcmd || echo fallback") == "fallback"
    # && must NOT run after a failure
    o, e, code = sh.run("nosuchcmd && echo should-not-appear")
    assert "should-not-appear" not in (o or "")
    # || must NOT run after a success
    o, e, code = sh.run("echo fine || echo should-not-appear")
    assert "should-not-appear" not in (o or "")


def test_pipes_still_work_with_chaining():
    sh, _ = _sh()
    sh.vos.write("/d.txt", "b\na\nb\n")
    assert out(sh, "cat /d.txt | sort | uniq") == "a\nb"
    assert out(sh, "echo x ; cat /d.txt | sort -u") == "a\nb"


# ------------------------------------------------------------------ history
def test_history_persists_across_restart():
    sh, base = _sh()
    sh.run("echo remember-this")
    sh.save_history()
    sh2, _ = _sh(base)
    assert "echo remember-this" in out(sh2, "history")


# ----------------------------------------------------- services over reboot
def test_services_survive_reboot():
    sh, _ = _sh()
    sh.run("service nginx start")
    assert "running" in out(sh, "service nginx status")
    sh.run("reboot")
    assert "running" in out(sh, "service nginx status"), "reboot wiped the service"
    assert "nginx" in out(sh, "ps")


def test_services_survive_restart():
    sh, base = _sh()
    sh.run("service nginx start")
    sh2, _ = _sh(base)
    assert "running" in out(sh2, "service nginx status")


def test_stopped_service_stays_stopped():
    sh, _ = _sh()
    sh.run("service nginx start")
    sh.run("service nginx stop")
    sh.run("reboot")
    assert "stopped" in out(sh, "service nginx status")


# ---------------------------------------------------------------- coreutils
def test_sort_variants():
    sh, _ = _sh()
    sh.vos.write("/n.txt", "10\n9\n100\n")
    assert out(sh, "sort /n.txt") == "10\n100\n9"
    assert out(sh, "sort -n /n.txt") == "9\n10\n100"
    assert out(sh, "sort -r -n /n.txt") == "100\n10\n9"
    sh.vos.write("/d.txt", "b\na\nb\n")
    assert out(sh, "sort -u /d.txt") == "a\nb"


def test_uniq_and_count():
    sh, _ = _sh()
    sh.vos.write("/d.txt", "a\na\nb\n")
    assert out(sh, "uniq /d.txt") == "a\nb"
    counted = out(sh, "cat /d.txt | uniq -c")
    assert "2 a" in counted and "1 b" in counted


def test_cut_fields():
    sh, _ = _sh()
    sh.vos.write("/p.csv", "alice:30:nyc\nbob:25:sf\n")
    assert out(sh, "cut -d: -f1 /p.csv") == "alice\nbob"
    assert out(sh, "cut -d: -f1,3 /p.csv") == "alice:nyc\nbob:sf"
    o, e, code = sh.run("cut /p.csv")
    assert code == 1


def test_tr_ranges_and_delete():
    sh, _ = _sh()
    assert out(sh, "echo hello | tr a-z A-Z") == "HELLO"
    assert out(sh, "echo a1b2 | tr 0-9 .") == "a.b."
    assert out(sh, "echo Hello | tr -d l") == "Heo"


def test_rev_seq_yes_truefalse():
    sh, _ = _sh()
    assert out(sh, "echo abc | rev") == "cba"
    assert out(sh, "seq 3") == "1\n2\n3"
    assert out(sh, "seq 2 4") == "2\n3\n4"
    assert out(sh, "seq 1 3 7") == "1\n4\n7"
    assert sh.run("true")[2] == 0
    assert sh.run("false")[2] == 1
    assert out(sh, "yes ok").splitlines()[0] == "ok"


def test_tee_writes_and_passes_through():
    sh, _ = _sh()
    assert out(sh, "echo piped | tee /out.txt") == "piped"
    assert sh.vos.read("/out.txt").strip() == "piped"
    sh.run("echo more | tee -a /out.txt")
    assert "piped" in sh.vos.read("/out.txt") and "more" in sh.vos.read("/out.txt")


def test_basename_dirname():
    sh, _ = _sh()
    assert out(sh, "basename /usr/local/bin/tool") == "tool"
    assert out(sh, "dirname /usr/local/bin/tool") == "/usr/local/bin"
    assert out(sh, "basename /usr/local/") == "local"
    assert out(sh, "dirname /single") == "/"


def test_stat_and_du():
    sh, _ = _sh()
    sh.vos.write("/f.txt", "12345")
    s = out(sh, "stat /f.txt")
    assert "/f.txt" in s and "Size: 5" in s and "regular file" in s
    o, e, code = sh.run("stat /nope")
    assert code == 1
    assert "/" in out(sh, "du -h /")


def test_coreutils_respect_the_sandbox():
    sh, _ = _sh()
    for cmd in ["sort ../../../../etc/passwd",
                "stat ../../../../etc/passwd",
                "du ../../../.."]:
        o, e, code = sh.run(cmd)
        assert code != 0 and "escapes" in (e or o), cmd


# ---------------------------------------------------------------------- man
def test_man_pages():
    sh, _ = _sh()
    page = out(sh, "man ls")
    assert "LS(1)" in page and "list files" in page
    assert "USAGE" in out(sh, "man snapshot")
    o, e, code = sh.run("man nosuchcommand")
    assert code == 1 and "no manual entry" in e
    o, e, code = sh.run("man")
    assert code == 1


def test_help_lists_the_new_commands():
    sh, _ = _sh()
    h = out(sh, "help")
    for cmd in ("sort", "uniq", "cut", "tr", "export", "alias", "man", "stat"):
        assert cmd in h, cmd


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
