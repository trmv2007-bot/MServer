"""Tests for users, groups and permissions."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mserver.agent.tools import build_tools  # noqa: E402
from mserver.vos import users as usermod  # noqa: E402
from mserver.vos.kernel import VOS, VOSPathError  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402
from mserver.vos.users import PermissionDenied  # noqa: E402


def new_env(as_user=None):
    d = tempfile.mkdtemp()
    vos = VOS(os.path.join(d, ".mserver"))
    shell = Shell(vos)
    if as_user:
        shell.run(f"su {as_user}")
    return vos, shell, d


class TestModeHelpers(unittest.TestCase):
    def test_mode_str_file(self):
        self.assertEqual(usermod.mode_str(0o644), "-rw-r--r--")

    def test_mode_str_dir(self):
        self.assertEqual(usermod.mode_str(0o755, True), "drwxr-xr-x")

    def test_mode_str_private(self):
        self.assertEqual(usermod.mode_str(0o600), "-rw-------")

    def test_parse_octal(self):
        self.assertEqual(usermod.parse_mode("755"), 0o755)

    def test_parse_symbolic_add(self):
        self.assertEqual(usermod.parse_mode("u+x", 0o644), 0o744)

    def test_parse_symbolic_remove(self):
        self.assertEqual(usermod.parse_mode("go-r", 0o644), 0o600)

    def test_parse_symbolic_equals(self):
        self.assertEqual(usermod.parse_mode("a=r", 0o777), 0o444)

    def test_parse_multiple_clauses(self):
        self.assertEqual(usermod.parse_mode("u+x,go-w", 0o666), 0o744)

    def test_parse_bad_mode(self):
        for bad in ["", "999", "u?x", "hello"]:
            with self.assertRaises(usermod.UserError, msg=bad):
                usermod.parse_mode(bad)


class TestAccounts(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, self.dir = new_env()

    def test_seeded_users_exist(self):
        names = [u["name"] for u in self.vos.users.users()]
        self.assertIn("root", names)
        self.assertIn("agent", names)
        self.assertIn("nobody", names)

    def test_root_is_uid_zero(self):
        self.assertEqual(self.vos.users.uid_of("root"), 0)

    def test_passwd_file_is_readable(self):
        self.assertIn("root:x:0:0:", self.vos.read("/etc/passwd"))

    def test_useradd(self):
        self.shell.run("useradd alice")
        self.assertIsNotNone(self.vos.users.get_user("alice"))

    def test_useradd_creates_home(self):
        self.shell.run("useradd alice")
        self.assertTrue(self.vos.is_dir("/home/alice"))

    def test_useradd_home_is_owned_by_user(self):
        self.shell.run("useradd alice")
        uid = self.vos.users.uid_of("alice")
        self.assertEqual(self.vos.users.meta("/home/alice")["uid"], uid)

    def test_uids_increment(self):
        self.shell.run("useradd alice")
        self.shell.run("useradd bob")
        self.assertNotEqual(self.vos.users.uid_of("alice"),
                            self.vos.users.uid_of("bob"))

    def test_duplicate_user_refused(self):
        self.shell.run("useradd alice")
        self.assertEqual(self.shell.run("useradd alice")[2], 1)

    def test_bad_user_name_refused(self):
        for bad in ["Alice", "9lives", "with space", "../evil"]:
            self.assertEqual(self.shell.run(f"useradd {bad}")[2], 1, bad)

    def test_userdel(self):
        self.shell.run("useradd alice")
        self.shell.run("userdel alice")
        self.assertIsNone(self.vos.users.get_user("alice"))

    def test_cannot_delete_root(self):
        self.assertEqual(self.shell.run("userdel root")[2], 1)

    def test_cannot_delete_current_user(self):
        self.shell.run("useradd alice")
        self.shell.run("su alice")
        with self.assertRaises(usermod.UserError):
            self.vos.users.del_user("alice")

    def test_users_command_lists_accounts(self):
        self.assertIn("agent", self.shell.run("users")[0])

    def test_id_command(self):
        self.assertIn("uid=0(root)", self.shell.run("id")[0])

    def test_id_of_other_user(self):
        self.assertIn("uid=1000(agent)", self.shell.run("id agent")[0])

    def test_id_unknown_user(self):
        self.assertEqual(self.shell.run("id nosuchuser")[2], 1)

    def test_accounts_persist(self):
        self.shell.run("useradd alice")
        vos2 = VOS(os.path.join(self.dir, ".mserver"))
        self.assertIsNotNone(vos2.users.get_user("alice"))


class TestSwitching(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, self.dir = new_env()

    def test_starts_as_root(self):
        self.assertEqual(self.shell.run("whoami")[0].strip(), "root")

    def test_enforcement_off_as_root(self):
        self.assertFalse(self.vos.users.enforce)

    def test_su_changes_user(self):
        self.shell.run("su agent")
        self.assertEqual(self.shell.run("whoami")[0].strip(), "agent")

    def test_su_enables_enforcement(self):
        self.shell.run("su agent")
        self.assertTrue(self.vos.users.enforce)

    def test_su_updates_env(self):
        self.shell.run("su agent")
        self.assertEqual(self.shell.env["USER"], "agent")
        self.assertEqual(self.shell.env["HOME"], "/home/agent")

    def test_su_unknown_user_fails(self):
        self.assertEqual(self.shell.run("su nosuchuser")[2], 1)

    def test_logout_returns_to_root(self):
        self.shell.run("su agent")
        self.shell.run("logout")
        self.assertEqual(self.shell.run("whoami")[0].strip(), "root")

    def test_logout_without_su_errors(self):
        self.assertEqual(self.shell.run("logout")[2], 1)

    def test_non_root_cannot_become_root(self):
        self.shell.run("su agent")
        _, err, code = self.shell.run("su root")
        self.assertEqual(code, 1)
        self.assertIn("Authentication failure", err)

    def test_nested_su(self):
        self.shell.run("useradd alice")
        self.shell.run("su agent")
        self.shell.run("su alice")
        self.assertEqual(self.shell.run("whoami")[0].strip(), "alice")
        self.shell.run("logout")
        self.assertEqual(self.shell.run("whoami")[0].strip(), "agent")


class TestEnforcement(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, self.dir = new_env(as_user="agent")

    def test_can_read_world_readable(self):
        self.assertEqual(self.shell.run("cat /etc/passwd")[2], 0)

    def test_cannot_write_etc(self):
        _, err, code = self.shell.run("echo x > /etc/hosts")
        self.assertEqual(code, 1)
        self.assertIn("Permission denied", err)

    def test_cannot_create_in_etc(self):
        self.assertEqual(self.shell.run("touch /etc/newfile")[2], 1)

    def test_cannot_remove_from_etc(self):
        self.shell.run("rm /etc/hosts")
        self.assertTrue(self.vos.exists("/etc/hosts"))

    def test_cannot_mkdir_in_etc(self):
        self.assertEqual(self.shell.run("mkdir /etc/newdir")[2], 1)

    def test_can_write_tmp(self):
        self.assertEqual(self.shell.run("echo ok > /tmp/mine")[2], 0)

    def test_can_read_own_write(self):
        self.shell.run("echo hello > /tmp/mine")
        self.assertIn("hello", self.shell.run("cat /tmp/mine")[0])

    def test_can_write_own_home(self):
        self.assertEqual(self.shell.run("echo x > /home/agent/notes")[2], 0)

    def test_cannot_read_root_private_file(self):
        self.shell.run("logout")
        self.shell.run("echo secret > /root/secret.txt")
        self.shell.run("chmod 600 /root/secret.txt")
        self.shell.run("su agent")
        _, err, code = self.shell.run("cat /root/secret.txt")
        self.assertEqual(code, 1)
        self.assertIn("Permission denied", err)

    def test_can_read_after_chmod_644(self):
        self.shell.run("logout")
        self.shell.run("echo open > /root/open.txt")
        self.shell.run("chmod 644 /root/open.txt")
        self.shell.run("su agent")
        self.assertIn("open", self.shell.run("cat /root/open.txt")[0])

    def test_non_root_cannot_useradd(self):
        self.assertEqual(self.shell.run("useradd bob")[2], 1)

    def test_non_root_cannot_chown(self):
        self.shell.run("echo x > /tmp/f")
        self.assertEqual(self.shell.run("chown root /tmp/f")[2], 1)

    def test_error_names_the_user(self):
        _, err, _ = self.shell.run("touch /etc/x")
        self.assertIn("agent", err)


class TestSudo(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, self.dir = new_env(as_user="agent")

    def test_sudo_allows_root_write(self):
        self.shell.run("sudo touch /etc/viasudo")
        self.assertTrue(self.vos.exists("/etc/viasudo"))

    def test_user_restored_after_sudo(self):
        self.shell.run("sudo touch /etc/x")
        self.assertEqual(self.vos.users.current, "agent")

    def test_enforcement_restored_after_sudo(self):
        self.shell.run("sudo touch /etc/x")
        self.assertTrue(self.vos.users.enforce)
        self.assertEqual(self.shell.run("touch /etc/y")[2], 1)

    def test_sudo_is_logged(self):
        self.shell.run("sudo touch /etc/x")
        self.assertIn("COMMAND=touch /etc/x", self.vos.read("/var/log/auth.log"))

    def test_sudo_without_command(self):
        self.assertEqual(self.shell.run("sudo")[2], 1)

    def test_restored_even_if_command_fails(self):
        self.shell.run("sudo nosuchcommand")
        self.assertEqual(self.vos.users.current, "agent")
        self.assertTrue(self.vos.users.enforce)


class TestChmodChown(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, self.dir = new_env()

    def test_chmod_octal(self):
        self.shell.run("echo x > /tmp/f")
        self.shell.run("chmod 600 /tmp/f")
        self.assertEqual(self.vos.users.meta("/tmp/f")["mode"], 0o600)

    def test_chmod_symbolic(self):
        self.shell.run("echo x > /tmp/f")
        self.shell.run("chmod u+x /tmp/f")
        self.assertTrue(self.vos.users.meta("/tmp/f")["mode"] & 0o100)

    def test_chmod_missing_file(self):
        self.assertIn("No such file", self.shell.run("chmod 600 /tmp/nope")[0])

    def test_chmod_bad_mode(self):
        self.shell.run("echo x > /tmp/f")
        self.assertEqual(self.shell.run("chmod 999 /tmp/f")[2], 1)

    def test_chown_changes_owner(self):
        self.shell.run("echo x > /tmp/f")
        self.shell.run("chown agent /tmp/f")
        self.assertEqual(self.vos.users.meta("/tmp/f")["uid"], 1000)

    def test_chown_unknown_user(self):
        self.shell.run("echo x > /tmp/f")
        self.assertEqual(self.shell.run("chown nosuchuser /tmp/f")[2], 1)

    def test_ls_l_shows_mode_and_owner(self):
        self.shell.run("echo x > /tmp/f")
        self.shell.run("chmod 600 /tmp/f")
        self.shell.run("chown agent /tmp/f")
        out = self.shell.run("ls -l /tmp")[0]
        self.assertIn("-rw-------", out)
        self.assertIn("agent", out)

    def test_owner_may_chmod_own_file(self):
        self.shell.run("su agent")
        self.shell.run("echo x > /tmp/mine")
        self.shell.run("chown agent /tmp/mine")  # refused, still root-owned
        self.shell.run("logout")
        self.shell.run("chown agent /tmp/mine")
        self.shell.run("su agent")
        self.assertNotIn("Not the owner", self.shell.run("chmod 600 /tmp/mine")[0])

    def test_non_owner_cannot_chmod(self):
        self.shell.run("echo x > /root/f")
        self.shell.run("su agent")
        self.assertIn("Not the owner", self.shell.run("chmod 777 /root/f")[0])

    def test_metadata_persists(self):
        self.shell.run("echo x > /tmp/f")
        self.shell.run("chmod 600 /tmp/f")
        vos2 = VOS(os.path.join(self.dir, ".mserver"))
        self.assertEqual(vos2.users.meta("/tmp/f")["mode"], 0o600)

    def test_metadata_forgotten_on_delete(self):
        """A new file must not inherit a deleted file's permissions."""
        self.shell.run("echo x > /tmp/f")
        self.shell.run("chmod 600 /tmp/f")
        self.shell.run("rm /tmp/f")
        self.shell.run("echo y > /tmp/f")
        self.assertEqual(self.vos.users.meta("/tmp/f")["mode"],
                         usermod.DEFAULT_FILE_MODE)

    def test_corrupt_metadata_does_not_crash(self):
        self.vos.write(usermod.PERMS_PATH, "{not json")
        self.assertEqual(self.vos.users.meta("/tmp")["mode"],
                         usermod.DEFAULT_DIR_MODE)


class TestSandboxStillFirst(unittest.TestCase):
    """Permissions are an inner layer; the sandbox must still win."""

    def setUp(self):
        self.vos, self.shell, self.dir = new_env(as_user="agent")

    def test_escape_refused_as_non_root(self):
        _, err, code = self.shell.run("cat ../../../../etc/passwd")
        self.assertEqual(code, 1)
        self.assertIn("escapes the virtual OS", err)

    def test_escape_refused_as_root(self):
        self.shell.run("logout")
        self.assertIn("escapes the virtual OS",
                      self.shell.run("cat ../../../../etc/passwd")[1])

    def test_sudo_does_not_grant_escape(self):
        """sudo raises vOS privilege, never host privilege."""
        _, err, _ = self.shell.run("sudo cat ../../../../etc/passwd")
        self.assertIn("escapes the virtual OS", err)

    def test_vpath_still_raises_for_escape(self):
        with self.assertRaises(VOSPathError):
            self.vos.vpath("/../../../etc/passwd")

    def test_permission_error_is_distinct_from_escape(self):
        """Two different failures must not be conflated."""
        with self.assertRaises(PermissionDenied):
            self.vos.write("/etc/hosts", "x")
        with self.assertRaises(VOSPathError):
            self.vos.vpath("../../etc/passwd")


class TestNoRecursion(unittest.TestCase):
    """The permission layer must not read its own metadata through itself."""

    def test_read_under_enforcement_terminates(self):
        vos, shell, _ = new_env(as_user="agent")
        self.assertTrue(vos.read("/etc/passwd"))

    def test_meta_of_permissions_file_terminates(self):
        vos, shell, _ = new_env(as_user="agent")
        self.assertIsInstance(vos.users.meta(usermod.PERMS_PATH), dict)

    def test_check_on_passwd_terminates(self):
        vos, shell, _ = new_env(as_user="agent")
        with self.assertRaises(PermissionDenied):
            vos.users.check("/etc/passwd", "w", "write")


class TestWhoamiTool(unittest.TestCase):
    def test_reports_root(self):
        vos, shell, _ = new_env()
        _, ex = build_tools(vos, shell, {})
        self.assertIn("root", ex["whoami"]({}))

    def test_warns_when_not_root(self):
        vos, shell, _ = new_env(as_user="agent")
        _, ex = build_tools(vos, shell, {})
        out = ex["whoami"]({})
        self.assertIn("agent", out)
        self.assertIn("NOT root", out)

    def test_agent_write_to_etc_refused(self):
        vos, shell, _ = new_env(as_user="agent")
        _, ex = build_tools(vos, shell, {})
        self.assertIn("error", ex["vos_write"](
            {"path": "/etc/evil", "content": "x"}).lower())

    def test_agent_can_still_write_tmp(self):
        vos, shell, _ = new_env(as_user="agent")
        _, ex = build_tools(vos, shell, {})
        ex["vos_write"]({"path": "/tmp/ok", "content": "x"})
        self.assertTrue(vos.exists("/tmp/ok"))


class TestServicesStillWork(unittest.TestCase):
    """Regression: the permission layer must not break existing features."""

    def test_pkg_install_as_root(self):
        vos, shell, _ = new_env()
        self.assertEqual(shell.run("pkg install nginx")[2], 0)

    def test_nginx_serves_as_non_root(self):
        vos, shell, _ = new_env()
        shell.run("pkg install nginx")
        shell.run("service nginx start")
        shell.run("su agent")
        self.assertIn("Hello from nginx", shell.run("curl http://mserver/")[0])

    def test_cron_still_ticks_as_non_root(self):
        vos, shell, _ = new_env()
        shell.run("crontab -a '* * * * *' 'logger still-works'")
        shell.run("su agent")
        vos.scheduler.tick()
        self.assertIn("still-works", vos.read("/var/log/syslog"))

    def test_snapshot_as_root_still_works(self):
        vos, shell, _ = new_env()
        self.assertEqual(shell.run("snapshot save test")[2], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
