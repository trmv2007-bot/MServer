"""Tests for cron and at — the vOS acting on its own."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mserver.agent.tools import build_tools  # noqa: E402
from mserver.vos import scheduler as cronmod  # noqa: E402
from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402


def new_env(start=False):
    d = tempfile.mkdtemp()
    vos = VOS(os.path.join(d, ".mserver"))
    shell = Shell(vos)
    if start:
        shell.run("service cron start")
    return vos, shell, d


def at_time(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class TestFieldParsing(unittest.TestCase):
    def test_star(self):
        self.assertEqual(cronmod.parse_field("*", 0, 5, "f"), {0, 1, 2, 3, 4, 5})

    def test_single(self):
        self.assertEqual(cronmod.parse_field("3", 0, 5, "f"), {3})

    def test_range(self):
        self.assertEqual(cronmod.parse_field("1-3", 0, 5, "f"), {1, 2, 3})

    def test_list(self):
        self.assertEqual(cronmod.parse_field("1,4", 0, 5, "f"), {1, 4})

    def test_step(self):
        self.assertEqual(cronmod.parse_field("*/2", 0, 5, "f"), {0, 2, 4})

    def test_range_with_step(self):
        self.assertEqual(cronmod.parse_field("0-4/2", 0, 5, "f"), {0, 2, 4})

    def test_out_of_range(self):
        with self.assertRaises(cronmod.CronError):
            cronmod.parse_field("9", 0, 5, "f")

    def test_bad_value(self):
        with self.assertRaises(cronmod.CronError):
            cronmod.parse_field("abc", 0, 5, "f")

    def test_zero_step(self):
        with self.assertRaises(cronmod.CronError):
            cronmod.parse_field("*/0", 0, 5, "f")

    def test_inverted_range(self):
        with self.assertRaises(cronmod.CronError):
            cronmod.parse_field("4-1", 0, 5, "f")


class TestSchedule(unittest.TestCase):
    def test_five_fields_required(self):
        with self.assertRaises(cronmod.CronError):
            cronmod.parse_schedule("* * *")

    def test_aliases(self):
        self.assertEqual(cronmod.parse_schedule("@hourly"),
                         cronmod.parse_schedule("0 * * * *"))

    def test_unknown_alias(self):
        with self.assertRaises(cronmod.CronError):
            cronmod.parse_schedule("@sometimes")

    def test_every_minute_matches_anything(self):
        s = cronmod.parse_schedule("* * * * *")
        self.assertTrue(cronmod.matches(s, time.localtime(at_time(2026, 9, 3, 13, 37))))

    def test_specific_hour_and_minute(self):
        s = cronmod.parse_schedule("30 2 * * *")
        self.assertTrue(cronmod.matches(s, time.localtime(at_time(2026, 9, 3, 2, 30))))
        self.assertFalse(cronmod.matches(s, time.localtime(at_time(2026, 9, 3, 2, 31))))
        self.assertFalse(cronmod.matches(s, time.localtime(at_time(2026, 9, 3, 3, 30))))

    def test_step_minutes(self):
        s = cronmod.parse_schedule("*/15 * * * *")
        for mi, want in ((0, True), (15, True), (7, False), (30, True)):
            self.assertEqual(
                cronmod.matches(s, time.localtime(at_time(2026, 9, 3, 1, mi))), want)

    def test_weekday_sunday_is_zero(self):
        """Cron counts Sunday as 0; Python counts Monday as 0."""
        s = cronmod.parse_schedule("0 0 * * 0")
        self.assertTrue(cronmod.matches(s, time.localtime(at_time(2026, 9, 6, 0, 0))))
        self.assertFalse(cronmod.matches(s, time.localtime(at_time(2026, 9, 7, 0, 0))))

    def test_dom_or_dow_when_both_restricted(self):
        """Real cron ORs day-of-month with day-of-week."""
        s = cronmod.parse_schedule("0 0 1 * 0")
        self.assertTrue(cronmod.matches(s, time.localtime(at_time(2026, 9, 1, 0, 0))))
        self.assertTrue(cronmod.matches(s, time.localtime(at_time(2026, 9, 6, 0, 0))))
        self.assertFalse(cronmod.matches(s, time.localtime(at_time(2026, 9, 8, 0, 0))))

    def test_month_field(self):
        s = cronmod.parse_schedule("0 0 1 1 *")
        self.assertTrue(cronmod.matches(s, time.localtime(at_time(2026, 1, 1, 0, 0))))
        self.assertFalse(cronmod.matches(s, time.localtime(at_time(2026, 2, 1, 0, 0))))

    def test_describe_is_readable(self):
        self.assertEqual(cronmod.describe("@hourly"), "hourly, on the hour")
        self.assertEqual(cronmod.describe("*/5 * * * *"), "every 5 minutes")


class TestAtTimeParsing(unittest.TestCase):
    def test_now(self):
        base = 1_000_000.0
        self.assertEqual(cronmod.parse_at_time("now", base), base)

    def test_relative_minutes(self):
        self.assertEqual(cronmod.parse_at_time("+5m", 1000.0), 1300.0)

    def test_relative_hours(self):
        self.assertEqual(cronmod.parse_at_time("+2h", 0.0), 7200.0)

    def test_relative_seconds(self):
        self.assertEqual(cronmod.parse_at_time("+30s", 0.0), 30.0)

    def test_bare_number_is_minutes(self):
        self.assertEqual(cronmod.parse_at_time("+3", 0.0), 180.0)

    def test_absolute_time_today(self):
        base = at_time(2026, 9, 3, 10, 0)
        self.assertEqual(cronmod.parse_at_time("17:30", base),
                         at_time(2026, 9, 3, 17, 30))

    def test_absolute_time_rolls_to_tomorrow(self):
        base = at_time(2026, 9, 3, 18, 0)
        self.assertEqual(cronmod.parse_at_time("09:00", base),
                         at_time(2026, 9, 4, 9, 0))

    def test_bad_time(self):
        for bad in ["", "lunchtime", "25:00", "+5years"]:
            with self.assertRaises(cronmod.CronError, msg=bad):
                cronmod.parse_at_time(bad, 0.0)


class TestCrontabCommand(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, self.dir = new_env()

    def test_empty_initially(self):
        self.assertIn("no cron jobs", self.shell.run("crontab -l")[0])

    def test_warns_when_crond_not_running(self):
        self.assertIn("crond is not running", self.shell.run("crontab -l")[0])

    def test_no_warning_when_running(self):
        self.shell.run("service cron start")
        self.assertNotIn("not running", self.shell.run("crontab -l")[0])

    def test_add_and_list(self):
        self.shell.run("crontab -a '*/5 * * * *' 'logger hi'")
        out = self.shell.run("crontab -l")[0]
        self.assertIn("*/5 * * * *", out)
        self.assertIn("logger hi", out)
        self.assertIn("every 5 minutes", out)

    def test_add_alias_is_expanded(self):
        self.shell.run("crontab -a @daily 'df'")
        self.assertIn("0 0 * * *", self.shell.run("crontab -l")[0])

    def test_add_bad_schedule_errors(self):
        _, err, code = self.shell.run("crontab -a 'nonsense' 'df'")
        self.assertEqual(code, 1)
        self.assertIn("5 fields", err)

    def test_add_out_of_range_errors(self):
        self.assertEqual(self.shell.run("crontab -a '99 * * * *' 'df'")[2], 1)

    def test_add_without_command_errors(self):
        self.assertEqual(self.shell.run("crontab -a '* * * * *'")[2], 1)

    def test_ids_are_sequential_not_line_numbers(self):
        self.shell.run("crontab -a '* * * * *' 'a'")
        self.shell.run("crontab -a '* * * * *' 'b'")
        jobs = self.vos.scheduler.list_jobs()
        self.assertEqual([j["id"] for j in jobs], [1, 2])

    def test_remove_by_id(self):
        self.shell.run("crontab -a '* * * * *' 'keep'")
        self.shell.run("crontab -a '* * * * *' 'drop'")
        self.shell.run("crontab -r 2")
        out = self.shell.run("crontab -l")[0]
        self.assertIn("keep", out)
        self.assertNotIn("drop", out)

    def test_remove_unknown_id_errors(self):
        self.assertEqual(self.shell.run("crontab -r 9")[2], 1)

    def test_remove_all(self):
        self.shell.run("crontab -a '* * * * *' 'a'")
        self.shell.run("crontab -r")
        self.assertIn("no cron jobs", self.shell.run("crontab -l")[0])

    def test_persists_across_restart(self):
        self.shell.run("crontab -a '* * * * *' 'survivor'")
        vos2 = VOS(os.path.join(self.dir, ".mserver"))
        self.assertIn("survivor", Shell(vos2).run("crontab -l")[0])

    def test_job_limit(self):
        sched = self.vos.scheduler
        for i in range(cronmod.MAX_JOBS):
            sched.add_job("* * * * *", f"logger {i}")
        with self.assertRaises(cronmod.CronError):
            sched.add_job("* * * * *", "one too many")

    def test_malformed_lines_are_skipped(self):
        self.vos.write(cronmod.CRONTAB_PATH,
                       "# comment\n\ngarbage\n* * * * * logger ok\n")
        jobs = self.vos.scheduler.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["command"], "logger ok")


class TestAtCommand(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, self.dir = new_env()

    def test_empty_initially(self):
        self.assertIn("no queued jobs", self.shell.run("at -l")[0])

    def test_queue_and_list(self):
        self.shell.run("at +10m 'logger later'")
        self.assertIn("logger later", self.shell.run("at -l")[0])

    def test_bad_time_errors(self):
        self.assertEqual(self.shell.run("at lunchtime 'df'")[2], 1)

    def test_cancel(self):
        self.shell.run("at +10m 'logger later'")
        self.shell.run("at -r 1")
        self.assertIn("no queued jobs", self.shell.run("at -l")[0])

    def test_cancel_unknown_errors(self):
        self.assertEqual(self.shell.run("at -r 7")[2], 1)

    def test_ids_increment(self):
        self.shell.run("at +10m 'a'")
        self.shell.run("at +20m 'b'")
        self.assertEqual([j["id"] for j in self.vos.scheduler.at_jobs()], [1, 2])

    def test_persists_across_restart(self):
        self.shell.run("at +30m 'survivor'")
        vos2 = VOS(os.path.join(self.dir, ".mserver"))
        self.assertIn("survivor", Shell(vos2).run("at -l")[0])

    def test_corrupt_queue_does_not_crash(self):
        self.vos.write(cronmod.ATJOBS_PATH, "{not json")
        self.assertEqual(self.vos.scheduler.at_jobs(), [])


class TestTicking(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, self.dir = new_env()
        self.sched = self.vos.scheduler

    def test_due_job_runs(self):
        self.shell.run("crontab -a '* * * * *' 'logger fired'")
        self.assertEqual(self.sched.tick(), 1)
        self.assertIn("fired", self.vos.read("/var/log/syslog"))

    def test_job_runs_once_per_minute(self):
        self.shell.run("crontab -a '* * * * *' 'logger once'")
        now = time.time()
        self.assertEqual(self.sched.tick(now), 1)
        self.assertEqual(self.sched.tick(now + 1), 0)
        self.assertEqual(self.sched.tick(now + 61), 1)

    def test_job_not_due_does_not_run(self):
        self.shell.run("crontab -a '0 3 * * *' 'logger nope'")
        self.assertEqual(self.sched.tick(at_time(2026, 9, 3, 4, 0)), 0)

    def test_at_job_runs_when_due(self):
        self.shell.run("at +0s 'logger at-ran'")
        self.assertEqual(self.sched.tick(time.time() + 1), 1)
        self.assertIn("at-ran", self.vos.read("/var/log/syslog"))

    def test_at_job_is_consumed(self):
        self.shell.run("at +0s 'logger once-only'")
        self.sched.tick(time.time() + 1)
        self.assertEqual(self.sched.at_jobs(), [])
        self.assertEqual(self.sched.tick(time.time() + 2), 0)

    def test_future_at_job_not_run(self):
        self.shell.run("at +1h 'logger later'")
        self.assertEqual(self.sched.tick(), 0)
        self.assertEqual(len(self.sched.at_jobs()), 1)

    def test_output_goes_to_cron_log(self):
        self.shell.run("crontab -a '* * * * *' 'echo hello-from-cron'")
        self.sched.tick()
        log = self.vos.read("/var/log/cron.log")
        self.assertIn("CMD echo hello-from-cron", log)
        self.assertIn("out: hello-from-cron", log)

    def test_failing_job_is_logged_not_fatal(self):
        self.shell.run("crontab -a '* * * * *' 'nosuchcommand'")
        self.assertEqual(self.sched.tick(), 1)
        self.assertIn("exit", self.vos.read("/var/log/cron.log"))

    def test_failing_job_does_not_stop_others(self):
        self.shell.run("crontab -a '* * * * *' 'nosuchcommand'")
        self.shell.run("crontab -a '* * * * *' 'logger survivor'")
        self.assertEqual(self.sched.tick(), 2)
        self.assertIn("survivor", self.vos.read("/var/log/syslog"))

    def test_job_does_not_disturb_interactive_cwd(self):
        """A background job must not move the user's working directory."""
        self.shell.run("cd /etc")
        self.shell.run("crontab -a '* * * * *' 'cd /tmp'")
        self.sched.tick()
        self.assertEqual(self.shell.cwd, "/etc")

    def test_job_runs_in_root_home(self):
        self.shell.run("crontab -a '* * * * *' 'pwd'")
        self.sched.tick()
        self.assertIn("/root", self.vos.read("/var/log/cron.log"))

    def test_run_counter(self):
        self.shell.run("crontab -a '* * * * *' 'logger x'")
        before = self.sched.runs
        self.sched.tick()
        self.assertEqual(self.sched.runs, before + 1)


class TestDestructiveJobsRefused(unittest.TestCase):
    """Scheduled work runs unattended, so the gate can never prompt."""

    def setUp(self):
        self.vos, self.shell, self.dir = new_env()

    def test_crontab_refuses_rm(self):
        _, err, code = self.shell.run("crontab -a '* * * * *' 'rm -rf /etc'")
        self.assertEqual(code, 1)
        self.assertIn("destructive", err)

    def test_at_refuses_rm(self):
        _, err, code = self.shell.run("at +5m 'rm /etc/hosts'")
        self.assertEqual(code, 1)
        self.assertIn("destructive", err)

    def test_refuses_rm_hidden_after_semicolon(self):
        self.assertEqual(
            self.shell.run("crontab -a '* * * * *' 'logger ok; rm -rf /etc'")[2], 1)

    def test_refuses_snapshot_rollback(self):
        self.assertEqual(
            self.shell.run("crontab -a '* * * * *' 'snapshot rollback x'")[2], 1)

    def test_planted_destructive_job_refused_at_run_time(self):
        """The crontab is a file; something may write to it directly."""
        self.vos.write(cronmod.CRONTAB_PATH, "* * * * * rm -rf /etc\n")
        self.assertTrue(self.vos.exists("/etc/hosts"))
        self.vos.scheduler.tick()
        self.assertTrue(self.vos.exists("/etc/hosts"))
        self.assertIn("REFUSED", self.vos.read("/var/log/cron.log"))

    def test_benign_job_still_runs(self):
        self.shell.run("crontab -a '* * * * *' 'logger benign'")
        self.assertEqual(self.vos.scheduler.tick(), 1)


class TestDaemon(unittest.TestCase):
    def test_service_start_runs_thread(self):
        vos, shell, _ = new_env()
        self.assertFalse(vos.scheduler.running())
        shell.run("service cron start")
        self.assertTrue(vos.scheduler.running())
        shell.run("service cron stop")
        self.assertFalse(vos.scheduler.running())

    def test_crond_appears_in_ps(self):
        vos, shell, _ = new_env(start=True)
        self.assertIn("crond", shell.run("ps")[0])
        shell.run("service cron stop")

    def test_listed_by_service_command(self):
        vos, shell, _ = new_env()
        self.assertIn("cron", shell.run("service")[0])

    def test_daemon_fires_a_job_unattended(self):
        """The whole point: something happens with nobody typing."""
        vos, shell, _ = new_env(start=True)
        try:
            shell.run("at +0s 'logger unattended'")
            deadline = time.time() + 20
            while time.time() < deadline:
                if "unattended" in vos.read("/var/log/syslog"):
                    break
                time.sleep(0.5)
            self.assertIn("unattended", vos.read("/var/log/syslog"))
        finally:
            shell.run("service cron stop")

    def test_restarts_on_reboot(self):
        vos, shell, _ = new_env(start=True)
        try:
            shell.run("reboot")
            self.assertTrue(vos.scheduler.running())
        finally:
            vos.scheduler.stop()

    def test_autostarts_on_fresh_boot_if_enabled(self):
        vos, shell, d = new_env(start=True)
        vos.scheduler.stop()
        vos2 = VOS(os.path.join(d, ".mserver"))
        try:
            self.assertTrue(vos2.scheduler.running())
        finally:
            vos2.scheduler.stop()

    def test_start_is_idempotent(self):
        vos, shell, _ = new_env(start=True)
        try:
            shell.run("service cron start")
            self.assertTrue(vos.scheduler.running())
        finally:
            shell.run("service cron stop")


class TestScheduleTool(unittest.TestCase):
    def setUp(self):
        self.vos, self.shell, _ = new_env()
        _, self.ex = build_tools(self.vos, self.shell, {})

    def test_list(self):
        self.assertIn("cron jobs", self.ex["schedule"]({"action": "list"}))

    def test_add_cron(self):
        out = self.ex["schedule"]({"action": "cron", "when": "*/5 * * * *",
                                   "command": "logger hi"})
        self.assertIn("added", out)
        self.assertIn("logger hi", self.shell.run("crontab -l")[0])

    def test_add_at(self):
        self.ex["schedule"]({"action": "at", "when": "+5m", "command": "df"})
        self.assertIn("df", self.shell.run("at -l")[0])

    def test_missing_fields(self):
        self.assertTrue(
            self.ex["schedule"]({"action": "cron"}).startswith("error:"))

    def test_remove(self):
        self.ex["schedule"]({"action": "cron", "when": "* * * * *",
                             "command": "logger x"})
        self.ex["schedule"]({"action": "remove", "when": "1"})
        self.assertIn("no cron jobs", self.shell.run("crontab -l")[0])

    def test_log(self):
        self.shell.run("service cron start")
        self.assertIsInstance(self.ex["schedule"]({"action": "log"}), str)
        self.shell.run("service cron stop")

    def test_bad_action(self):
        self.assertTrue(
            self.ex["schedule"]({"action": "explode"}).startswith("error:"))

    def test_destructive_refused_through_tool(self):
        out = self.ex["schedule"]({"action": "cron", "when": "* * * * *",
                                   "command": "rm -rf /"})
        self.assertIn("destructive", out)

    def test_command_is_quoted_as_one_word(self):
        """A command with spaces must not be split into extra arguments."""
        self.ex["schedule"]({"action": "cron", "when": "* * * * *",
                             "command": "echo one two three"})
        jobs = self.vos.scheduler.list_jobs()
        self.assertEqual(jobs[0]["command"], "echo one two three")


if __name__ == "__main__":
    unittest.main(verbosity=2)
