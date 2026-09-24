"""Integration arms against a real DynamoDB API.

Skipped unless SANDBOX_LOCK_TABLE is set. Each test uses its own random --key, so
running against the LIVE table never touches the real `sandbox` item.
"""
import os
import secrets
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.environ.get("SANDBOX_LOCK_TABLE")


def lock(cmd, role, holder, key, *extra):
    argv = [sys.executable, os.path.join(HERE, "sandbox_lock.py"), cmd, "--role", role, "--holder", holder,
            "--table", TABLE, "--key", key, "--poll", "1", *extra]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


@unittest.skipUnless(TABLE, "set SANDBOX_LOCK_TABLE to run integration arms")
class Arms(unittest.TestCase):
    def setUp(self):
        self.key = "selftest-" + secrets.token_hex(4)

    def test_forced_overlap_writer_waits_names_reader_then_proceeds(self):
        self.assertEqual(lock("acquire", "reader", "rA", self.key).returncode, 0)
        blocked = lock("acquire", "writer", "W", self.key, "--max-wait", "3")
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("reader rA", blocked.stdout)
        self.assertEqual(lock("release", "reader", "rA", self.key, "--expect-held").returncode, 0)
        self.assertEqual(lock("acquire", "writer", "W", self.key, "--max-wait", "10").returncode, 0)
        self.assertEqual(lock("release", "writer", "W", self.key, "--expect-held").returncode, 0)

    def test_writer_holding_blocks_reader(self):
        self.assertEqual(lock("acquire", "writer", "W", self.key).returncode, 0)
        blocked = lock("acquire", "reader", "rA", self.key, "--max-wait", "2")
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("writer W", blocked.stdout)
        lock("release", "writer", "W", self.key)

    def test_crashed_reader_lease_lapses_and_writer_gets_in(self):
        self.assertEqual(lock("acquire", "reader", "rDead", self.key, "--lease", "2").returncode, 0)
        time.sleep(3)
        self.assertEqual(lock("acquire", "writer", "W", self.key, "--max-wait", "5").returncode, 0)
        lock("release", "writer", "W", self.key)

    def test_lapsed_holder_release_reports_lease_lost(self):
        self.assertEqual(lock("acquire", "reader", "rSlow", self.key, "--lease", "2").returncode, 0)
        time.sleep(3)
        self.assertEqual(lock("acquire", "writer", "W", self.key, "--max-wait", "5").returncode, 0)
        out = lock("release", "reader", "rSlow", self.key, "--expect-held")
        self.assertEqual(out.returncode, 3, out.stdout)
        self.assertIn("LEASE LOST", out.stdout)
        lock("release", "writer", "W", self.key)

    def test_lost_race_is_a_conflict(self):
        sys.path.insert(0, HERE)
        import sandbox_lock as sl
        a, b = sl.Store(TABLE, self.key), sl.Store(TABLE, self.key)
        va, sa = a.read()
        vb, sb = b.read()
        b.write(vb, sb)
        with self.assertRaises(sl.Conflict):
            a.write(va, sa)

    def test_missing_table_is_unreadable_not_held(self):
        argv = [sys.executable, os.path.join(HERE, "sandbox_lock.py"), "acquire", "--role", "reader",
                "--holder", "r", "--table", TABLE + "-does-not-exist", "--key", self.key, "--max-wait", "1"]
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertIn("LOCK UNREADABLE", out.stdout)
        self.assertNotIn("held by", out.stdout)


if __name__ == "__main__":
    unittest.main()
