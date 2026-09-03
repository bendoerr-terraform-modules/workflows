import unittest

from canary_completeness import declared, drifted, passed

CALLEE = """
on:
  workflow_call:
    inputs:
      a:
        type: string
        default: ""
      b:
        type: boolean
        required: true
"""

CALLER = """
on:
  pull_request:
jobs:
  max-x:
    uses: ./.github/workflows/x.yml
    with:
      a: "v"
      b: true
  min-x:
    uses: ./.github/workflows/x.yml
    with:
      b: true
  unrelated:
    uses: ./.github/workflows/other.yml
"""


class TestDeclared(unittest.TestCase):
    def test_none_when_not_reusable(self):
        self.assertIsNone(declared("on:\n  pull_request:\n"))

    def test_splits_all_and_required(self):
        all_i, req = declared(CALLEE)
        self.assertEqual(all_i, {"a", "b"})
        self.assertEqual(req, {"b"})

    def test_reusable_with_no_inputs_is_empty_not_none(self):
        self.assertEqual(declared("on:\n  workflow_call:\n"), (set(), set()))


class TestPassed(unittest.TestCase):
    def test_collects_per_job_for_the_named_callee_only(self):
        got = passed(CALLER, "./.github/workflows/x.yml")
        self.assertEqual(set(got), {"max-x", "min-x"})
        self.assertEqual(got["max-x"], {"a", "b"})
        self.assertEqual(got["min-x"], {"b"})

    def test_job_with_no_with_block_passes_nothing(self):
        src = ("on:\n  pull_request:\njobs:\n  j:\n"
               "    uses: ./.github/workflows/x.yml\n")
        self.assertEqual(passed(src, "./.github/workflows/x.yml"), {"j": set()})

    def test_remote_uses_does_not_count_as_a_canary(self):
        # LOCALITY IS THE MECHANISM: actionlint only checks an interface when the
        # `uses:` resolves on disk. A canary rewritten to a remote ref must stop
        # counting, so its reusable reports DRIFT rather than silently passing.
        src = ("on:\n  pull_request:\njobs:\n  max-x:\n"
               "    uses: owner/repo/.github/workflows/x.yml@main\n"
               "    with:\n      a: \"v\"\n")
        self.assertEqual(passed(src, "./.github/workflows/x.yml"), {})

    def test_no_matching_callee_yields_nothing(self):
        self.assertEqual(passed(CALLER, "./.github/workflows/absent.yml"), {})


class TestDrifted(unittest.TestCase):
    """The DRIFT decisions themselves. Previously these were only exercised by
    forcing a red against the live tree -- a real arm, but not a unit row, so a
    refactor could change the rule with every unit test still green."""

    def test_exact_max_and_min_is_clean(self):
        self.assertEqual(drifted({"a", "b"}, {"b"},
                                 {"max": {"a", "b"}, "min": {"b"}}), [])

    def test_missing_input_in_max_is_drift(self):
        msgs = drifted({"a", "b"}, set(), {"max": {"a"}, "min": set()})
        self.assertEqual(len(msgs), 1)
        self.assertIn("'b'", msgs[0].replace('"', "'"))

    def test_no_canary_at_all_is_drift(self):
        msgs = drifted({"a"}, set(), {})
        self.assertEqual(len(msgs), 1)
        self.assertIn("no canary", msgs[0])

    def test_missing_min_is_drift_when_max_passes_inputs(self):
        msgs = drifted({"a", "b"}, {"b"}, {"max": {"a", "b"}})
        self.assertEqual(len(msgs), 1)
        self.assertIn("MIN", msgs[0])

    def test_zero_input_reusable_needs_no_min(self):
        # Where MAX passes nothing it already IS the MIN.
        self.assertEqual(drifted(set(), set(), {"max": set()}), [])

    def test_equal_sized_but_wrong_sets_do_not_pass_on_a_tiebreak(self):
        # max(key=len) would pick arbitrarily here; the verdict must be exact.
        msgs = drifted({"a", "b"}, set(), {"c1": {"a", "x"}, "c2": {"b", "y"}})
        self.assertTrue(msgs)


if __name__ == "__main__":
    unittest.main()
