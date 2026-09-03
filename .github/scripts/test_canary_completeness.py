import unittest

from canary_completeness import declared, passed

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


if __name__ == "__main__":
    unittest.main()
