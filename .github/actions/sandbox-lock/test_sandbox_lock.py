import contextlib
import io
import json
import subprocess
import unittest

import sandbox_lock as sl

LEASE = 600


class TryAcquireReader(unittest.TestCase):
    def test_empty_lock_grants_reader(self):
        new, ok, blockers = sl.try_acquire(sl.empty_state(), "r1", "reader", 1000, LEASE)
        self.assertTrue(ok)
        self.assertEqual(blockers, [])
        self.assertEqual(new["readers"], {"r1": 1600})

    def test_readers_share(self):
        s = {"readers": {"r1": 1600}, "writer": None, "intent": None}
        new, ok, _ = sl.try_acquire(s, "r2", "reader", 1000, LEASE)
        self.assertTrue(ok)
        self.assertEqual(set(new["readers"]), {"r1", "r2"})

    def test_live_writer_blocks_reader_and_is_named(self):
        s = {"readers": {}, "writer": {"holder": "nuke", "exp": 1600}, "intent": None}
        new, ok, blockers = sl.try_acquire(s, "r1", "reader", 1000, LEASE)
        self.assertFalse(ok)
        self.assertIsNone(new)
        self.assertEqual(blockers, ["writer nuke"])

    def test_live_intent_blocks_reader(self):
        s = {"readers": {}, "writer": None, "intent": {"holder": "nuke", "exp": 1600}}
        _, ok, blockers = sl.try_acquire(s, "r1", "reader", 1000, LEASE)
        self.assertFalse(ok)
        self.assertEqual(blockers, ["waiting writer nuke"])

    def test_expired_writer_does_not_block(self):
        s = {"readers": {}, "writer": {"holder": "nuke", "exp": 999}, "intent": None}
        new, ok, _ = sl.try_acquire(s, "r1", "reader", 1000, LEASE)
        self.assertTrue(ok)
        self.assertIsNone(new["writer"])

    def test_lease_expiring_exactly_now_is_expired(self):
        s = {"readers": {}, "writer": {"holder": "nuke", "exp": 1000}, "intent": None}
        _, ok, _ = sl.try_acquire(s, "r1", "reader", 1000, LEASE)
        self.assertTrue(ok)


    def test_reader_expiring_exactly_now_is_pruned(self):
        s = {"readers": {"old": 1000}, "writer": None, "intent": None}
        self.assertEqual(sl.prune(s, 1000)["readers"], {})


class TryAcquireWriter(unittest.TestCase):
    def test_empty_lock_grants_writer_and_clears_own_intent(self):
        s = {"readers": {}, "writer": None, "intent": {"holder": "w", "exp": 1600}}
        new, ok, _ = sl.try_acquire(s, "w", "writer", 1000, LEASE)
        self.assertTrue(ok)
        self.assertEqual(new["writer"], {"holder": "w", "exp": 1600})
        self.assertIsNone(new["intent"])

    def test_live_reader_blocks_writer_and_sets_intent(self):
        s = {"readers": {"r1": 1600}, "writer": None, "intent": None}
        new, ok, blockers = sl.try_acquire(s, "w", "writer", 1000, LEASE)
        self.assertFalse(ok)
        self.assertEqual(blockers, ["reader r1"])
        self.assertEqual(new["intent"], {"holder": "w", "exp": 1600})
        self.assertIsNone(new["writer"])

    def test_waiting_writer_refreshes_its_intent(self):
        s = {"readers": {"r1": 1900}, "writer": None, "intent": {"holder": "w", "exp": 1100}}
        new, ok, _ = sl.try_acquire(s, "w", "writer", 1000, LEASE)
        self.assertFalse(ok)
        self.assertEqual(new["intent"]["exp"], 1600)

    def test_other_writers_intent_is_not_overwritten(self):
        s = {"readers": {}, "writer": None, "intent": {"holder": "w1", "exp": 1600}}
        new, ok, blockers = sl.try_acquire(s, "w2", "writer", 1000, LEASE)
        self.assertFalse(ok)
        self.assertIsNone(new)
        self.assertEqual(blockers, ["waiting writer w1"])

    def test_live_writer_blocks_other_writer(self):
        s = {"readers": {}, "writer": {"holder": "w1", "exp": 1600}, "intent": None}
        _, ok, blockers = sl.try_acquire(s, "w2", "writer", 1000, LEASE)
        self.assertFalse(ok)
        self.assertEqual(blockers[0], "writer w1")

    def test_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            sl.try_acquire(sl.empty_state(), "x", "admin", 1000, LEASE)


class RenewAndRelease(unittest.TestCase):
    def test_renew_extends_live_reader(self):
        s = {"readers": {"r1": 1100}, "writer": None, "intent": None}
        new, held = sl.renew(s, "r1", "reader", 1000, LEASE)
        self.assertTrue(held)
        self.assertEqual(new["readers"]["r1"], 1600)

    def test_renew_reports_lapsed_reader_as_not_held(self):
        s = {"readers": {"r1": 999}, "writer": None, "intent": None}
        new, held = sl.renew(s, "r1", "reader", 1000, LEASE)
        self.assertFalse(held)
        self.assertIsNone(new)

    def test_renew_extends_own_writer_only(self):
        s = {"readers": {}, "writer": {"holder": "w1", "exp": 1100}, "intent": None}
        self.assertTrue(sl.renew(s, "w1", "writer", 1000, LEASE)[1])
        self.assertFalse(sl.renew(s, "w2", "writer", 1000, LEASE)[1])

    def test_renew_does_not_mutate_its_caller(self):
        # prune() used to carry writer/intent through BY REFERENCE, so renew()'s
        # `s["writer"]["exp"] = now + lease` reached back into the caller's dict.
        # LEASE is 600, so now(1000) + LEASE = 1600. Both assertions below use that;
        # an earlier draft of this arm asserted 1100 and was RED on the FIXED code.
        # The fixture's 1050 is chosen only so the returned and caller values differ
        # VISIBLY -- kitten's simpler form (fixture exp=1100, assert the caller is
        # still 1100) discriminates just as well: measured against both prune()
        # versions, old -> caller 1600 (fails, catches the bug), new -> 1100 (passes).
        s = {"readers": {}, "writer": {"holder": "w1", "exp": 1050}, "intent": None}
        new, held = sl.renew(s, "w1", "writer", 1000, LEASE)
        self.assertTrue(held)
        self.assertEqual(new["writer"]["exp"], 1600)   # the RETURNED state advances
        self.assertEqual(s["writer"]["exp"], 1050)     # the CALLER'S state must not

    def test_release_removes_reader(self):
        s = {"readers": {"r1": 1600, "r2": 1600}, "writer": None, "intent": None}
        new, held = sl.release(s, "r1", "reader", 1000)
        self.assertTrue(held)
        self.assertEqual(new["readers"], {"r2": 1600})

    def test_release_clears_writer_and_own_intent(self):
        s = {"readers": {}, "writer": {"holder": "w", "exp": 1600}, "intent": {"holder": "w", "exp": 1600}}
        new, held = sl.release(s, "w", "writer", 1000)
        self.assertTrue(held)
        self.assertIsNone(new["writer"])
        self.assertIsNone(new["intent"])

    def test_release_of_lapsed_holder_is_not_held(self):
        s = {"readers": {"r1": 999}, "writer": None, "intent": None}
        _, held = sl.release(s, "r1", "reader", 1000)
        self.assertFalse(held)


def completed(rc, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


class StoreAgainstFakeCli(unittest.TestCase):
    def test_missing_item_reads_as_empty_unversioned(self):
        store = sl.Store("t", "sandbox", run=lambda *a, **k: completed(0, ""))
        self.assertEqual(store.read(), (None, sl.empty_state()))

    def test_item_is_parsed(self):
        item = {"Item": {"pk": {"S": "sandbox"}, "lock_version": {"N": "7"},
                         "lock_state": {"S": json.dumps({"readers": {"r": 5}, "writer": None, "intent": None})}}}
        store = sl.Store("t", "sandbox", run=lambda *a, **k: completed(0, json.dumps(item)))
        version, state = store.read()
        self.assertEqual(version, 7)
        self.assertEqual(state["readers"], {"r": 5})

    def test_cli_failure_is_unreadable_not_empty(self):
        store = sl.Store("t", "sandbox", run=lambda *a, **k: completed(254, err="AccessDeniedException"))
        with self.assertRaises(sl.Unreadable):
            store.read()

    def test_malformed_item_is_unreadable(self):
        store = sl.Store("t", "sandbox", run=lambda *a, **k: completed(0, '{"Item": {"pk": {"S": "sandbox"}}}'))
        with self.assertRaises(sl.Unreadable):
            store.read()

    def test_valid_json_wrong_shape_is_unreadable(self):
        bad = {"Item": {"pk": {"S": "sandbox"}, "lock_version": {"N": "2"},
                        "lock_state": {"S": json.dumps({"readers": {}, "writer": {"holder": "w"}, "intent": None})}}}
        store = sl.Store("t", "sandbox", run=lambda *a, **k: completed(0, json.dumps(bad)))
        with self.assertRaises(sl.Unreadable):
            store.read()

    def test_conditional_failure_is_conflict(self):
        err = "An error occurred (ConditionalCheckFailedException) when calling the PutItem operation"
        store = sl.Store("t", "sandbox", run=lambda *a, **k: completed(254, err=err))
        with self.assertRaises(sl.Conflict):
            store.write(3, sl.empty_state())

    def test_first_write_conditions_on_absence_later_on_version(self):
        calls = []
        store = sl.Store("t", "sandbox", run=lambda argv, **k: calls.append(argv) or completed(0))
        store.write(None, sl.empty_state())
        store.write(4, sl.empty_state())
        self.assertIn("attribute_not_exists(pk)", calls[0])
        self.assertIn("#v = :v", calls[1])
        item = json.loads(calls[1][calls[1].index("--item") + 1])
        self.assertEqual(item["lock_version"], {"N": "5"})


class FakeStore:
    def __init__(self, conflicts=0):
        self.version, self.state, self.conflicts, self.writes = None, sl.empty_state(), conflicts, 0

    def read(self):
        return self.version, json.loads(json.dumps(self.state))

    def write(self, version, state):
        if self.conflicts:
            self.conflicts -= 1
            raise sl.Conflict()
        assert version == self.version
        self.version, self.state, self.writes = (version or 0) + 1, state, self.writes + 1


class Transact(unittest.TestCase):
    def test_retries_through_conflicts(self):
        store = FakeStore(conflicts=2)
        result = sl.transact(store, lambda s: (s, "ok"), sleep=lambda _: None)
        self.assertEqual(result, "ok")
        self.assertEqual(store.writes, 1)

    def test_gives_up_as_contended_not_unreadable(self):
        store = FakeStore(conflicts=99)
        with self.assertRaises(sl.Contended):
            sl.transact(store, lambda s: (s, "ok"), attempts=3, sleep=lambda _: None)

    def test_no_write_when_fn_returns_none(self):
        store = FakeStore()
        sl.transact(store, lambda s: (None, "blocked"), sleep=lambda _: None)
        self.assertEqual(store.writes, 0)


class Commands(unittest.TestCase):
    def test_acquire_times_out_naming_holders_and_clears_its_intent(self):
        store = FakeStore()
        store.state = {"readers": {"r1": 10**10}, "writer": None, "intent": None}
        clock = iter(range(1000, 100000, 50))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sl.cmd_acquire(store, "w", "writer", lease=600, max_wait=100, poll=20,
                                clock=lambda: next(clock), sleep=lambda _: None)
        self.assertEqual(rc, sl.EXIT_TIMEOUT)
        self.assertIn("TIMED OUT", out.getvalue())
        self.assertIn("reader r1", out.getvalue())
        self.assertIsNone(store.state["intent"])

    def test_contention_is_waiting_then_timeout_never_unreadable(self):
        store = FakeStore(conflicts=10**6)
        clock = iter(range(1000, 100000, 50))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sl.cmd_acquire(store, "r", "reader", lease=600, max_wait=100, poll=20,
                                clock=lambda: next(clock), sleep=lambda _: None)
        self.assertEqual(rc, sl.EXIT_TIMEOUT)
        self.assertNotIn("UNREADABLE", out.getvalue())
        self.assertIn("contended", out.getvalue())

    def test_acquire_reports_who_else_holds(self):
        store = FakeStore()
        store.state = {"readers": {"r1": 10**10}, "writer": None, "intent": None}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sl.cmd_acquire(store, "r2", "reader", lease=600, max_wait=100, poll=20,
                                clock=lambda: 1000, sleep=lambda _: None)
        self.assertEqual(rc, sl.EXIT_OK)
        self.assertIn("also holding: reader r1", out.getvalue())

    def test_release_expecting_held_but_lapsed_is_lease_lost(self):
        store = FakeStore()
        rc = sl.cmd_release(store, "r1", "reader", lost_file=None, expect_held=True, clock=lambda: 1000)
        self.assertEqual(rc, sl.EXIT_LOST)

    def test_renew_loop_marks_lost_when_entry_vanishes(self):
        import os, tempfile
        store = FakeStore()
        lost = os.path.join(tempfile.mkdtemp(), "lost")
        rc = sl.cmd_renew_loop(store, "r1", "reader", lease=600, interval=120, lost_file=lost,
                               clock=lambda: 1000, sleep=lambda _: None, max_iterations=1)
        self.assertEqual(rc, sl.EXIT_LOST)
        self.assertTrue(os.path.exists(lost))


class MainVerdicts(unittest.TestCase):
    def test_bad_arguments_exit_usage_not_unreadable(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            sl.main(["acquire", "--role", "admin", "--holder", "h"])
        self.assertEqual(cm.exception.code, sl.EXIT_USAGE)

    def test_crash_is_unreadable_not_timed_out(self):
        def boom(*a, **k):
            raise FileNotFoundError("aws")
        original = sl.subprocess.run
        sl.subprocess.run = boom
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = sl.main(["acquire", "--role", "reader", "--holder", "h", "--max-wait", "0"])
        finally:
            sl.subprocess.run = original
        self.assertEqual(rc, sl.EXIT_UNREADABLE)
        self.assertIn("LOCK UNREADABLE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
