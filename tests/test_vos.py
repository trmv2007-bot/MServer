"""vOS kernel + shell tests. Run: python3 tests/test_vos.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mserver.vos.kernel import VOS, VOSPathError  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402


def make():
    tmp = tempfile.mkdtemp(prefix="mserver-test-")
    vos = VOS(tmp)
    return vos, Shell(vos)


def test_boot_base_files():
    vos, shell = make()
    assert vos.exists("/etc/os-release")
    assert "MServerOS" in vos.read("/etc/os-release")
    assert vos.is_dir("/usr/bin")
    assert "mserver" in vos.read("/etc/hostname")
    assert len(vos.processes) >= 3  # init, mserver, mserver-agent


def test_path_confinement():
    vos, _ = make()
    for bad in ("/../etc/passwd", "/root/../../x", "/tmp/../../../x", "a/../../.."):
        try:
            vos.vpath(bad)
            raise AssertionError(f"{bad!r} should have been rejected")
        except VOSPathError:
            pass
    # inside paths stay fine
    assert str(vos.vpath("/root/../etc")) == str(vos.vpath("/etc"))


def test_fs_roundtrip():
    vos, shell = make()
    vos.write("/root/notes.txt", "hello")
    assert vos.read("/root/notes.txt") == "hello"
    vos.append("/root/notes.txt", "\nworld")
    assert vos.read("/root/notes.txt") == "hello\nworld"
    names = [e["name"] for e in vos.listdir("/root")]
    assert "notes.txt" in names
    vos.copy("/root/notes.txt", "/root/notes2.txt")
    assert vos.read("/root/notes2.txt") == "hello\nworld"
    vos.move("/root/notes2.txt", "/root/renamed.txt")
    assert vos.exists("/root/renamed.txt")
    vos.remove("/root/notes.txt")
    assert not vos.exists("/root/notes.txt")
    vos.remove("/root/renamed.txt")


def test_shell_basics():
    vos, shell = make()
    out, err, code = shell.run(
        "mkdir /var/www; echo hello > /var/www/index.txt; cat /var/www/index.txt"
    )
    assert code == 0 and out.strip() == "hello", (out, err)

    out, err, code = shell.run("ls /var/www")
    assert "index.txt" in out

    out, err, code = shell.run("cat /etc/hosts | grep mserver")
    assert code == 0 and "mserver" in out, (out, err)
    out, err, code = shell.run("echo a b c | grep zzz")
    assert code == 1 and out == "", (out, err)

    out, err, code = shell.run("cd /var; pwd")
    assert out.strip() == "/var", (out, err)

    out, err, code = shell.run("echo one two three | wc")
    assert code == 0 and out.split()[0] == "1"

    out, err, code = shell.run("echo >> /var/www/index.txt second")
    assert code == 0
    assert "second" in vos.read("/var/www/index.txt")

    out, err, code = shell.run("ls /var/www/*")
    assert "index.txt" in out

    out, err, code = shell.run("no-such-command")
    assert code == 127


def test_shell_glob_and_find():
    vos, shell = make()
    for f in ("a.log", "b.log", "c.txt"):
        vos.write(f"/var/log/{f}", f)
    out, _, _ = shell.run("ls /var/log/*.log")
    assert "a.log" in out and "b.log" in out and "c.txt" not in out
    out, _, _ = shell.run("find /var/log -name *.log")
    assert "a.log" in out and "c.txt" not in out


def test_packages():
    vos, shell = make()
    out, err, code = shell.run("pkg install cowsay")
    assert code == 0 and "OK" in out, (out, err)
    assert "cowsay" in vos.installed_packages()
    out, err, code = shell.run("cowsay hello agent")
    assert code == 0 and "< hello agent >" in out, (out, err)
    out, err, code = shell.run("pkg list")
    assert "cowsay" in out
    out, err, code = shell.run("pkg remove cowsay")
    assert code == 0
    assert "cowsay" not in vos.installed_packages()
    out, err, code = shell.run("cowsay x")
    assert code == 127


def test_packages_persist():
    tmp = tempfile.mkdtemp(prefix="mserver-test-")
    vos = VOS(tmp)
    sh = Shell(vos)
    sh.run("pkg install figlet")
    sh2 = Shell(vos)  # fresh shell, same disk
    assert "figlet" in vos.installed_packages()
    out, _, code = sh2.run("figlet ok")
    assert code == 0


def test_services():
    vos, shell = make()
    out, _, code = shell.run("service ssh start")
    assert code == 0 and "started" in out
    out, _, code = shell.run("ps")
    assert "sshd" in out
    out, _, code = shell.run("service ssh status")
    assert "running" in out
    out, _, code = shell.run("service ssh stop")
    assert code == 0 and "stopped" in out
    out, _, code = shell.run("service ssh status")
    assert "stopped" in out


def test_search():
    vos, shell = make()
    vos.write("/etc/ssl/cnf.txt", "alpha\nbeta\n")
    hits = vos.search("beta", "/etc")
    assert hits and hits[0][0] == "/etc/ssl/cnf.txt"
    assert vos.search("zzz-nope", "/") == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
