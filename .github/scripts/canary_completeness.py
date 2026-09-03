#!/usr/bin/env python3
"""Assert every reusable still has canaries that exercise its whole interface.

WHY: actionlint does the real break detection -- it validates a LOCAL `uses:`
against the callee's workflow_call interface and reds on a renamed input, a
removed one, a type change, a new required input, or a vanished reusable file.
But it can only check what a canary actually passes. A canary that quietly drops
an input narrows its own coverage and keeps reading green: the failure mode is
an absence, not a red.

Rules, per reusable under .github/workflows:
  - some canary job must pass EXACTLY the declared input set (the MAX canary)
  - some canary job must pass EXACTLY the required set (the MIN canary), and
    that is required ONLY where MAX passes at least one input -- where MAX
    passes nothing it already IS the MIN.

MAX catches rename/removal/type-change/new-required. MIN catches an existing
optional input flipped to required, which MAX cannot see by construction: a
caller that omits nothing cannot be caught omitting something.

Exit: 0 complete - 1 a canary drifted - 2 could not measure.
A 2 is never silently a 0: an unreadable tree is not a clean bill of health.
"""
import pathlib
import sys

import yaml

WF = pathlib.Path(".github/workflows")
CANARY_FILES = ["canaries.yml", "self-lint.yml"]
# GitHub accepts BOTH extensions. A .yml-only glob silently shrinks the
# population -- the exact hole the denominator print below exists to catch.
WORKFLOW_GLOB = "*.y*ml"


def _on(doc):
    # PyYAML parses a bare `on:` key as the boolean True (YAML 1.1). Accept both.
    return doc.get("on", doc.get(True)) if isinstance(doc, dict) else None


def declared(workflow_yaml):
    """Return (all_inputs, required_inputs) or None if not a reusable."""
    doc = yaml.safe_load(workflow_yaml)
    on = _on(doc)
    if not isinstance(on, dict) or "workflow_call" not in on:
        return None
    ins = ((on.get("workflow_call") or {}).get("inputs")) or {}
    return set(ins), {k for k, v in ins.items() if (v or {}).get("required")}


def passed(caller_yaml, callee_path):
    """job name -> set of input names that job passes to callee_path.

    The comparison is an EXACT match on the LOCAL path ("./.github/..."), and
    that is load-bearing rather than incidental. actionlint only validates a
    reusable's interface when the `uses:` resolves on disk; a canary rewritten to
    a remote `uses: owner/repo/.github/workflows/x.yml@ref` would be silently
    unchecked. Because a remote ref cannot equal callee_path, such a canary stops
    counting here and its reusable reports DRIFT ("no canary calls it") instead
    of quietly passing. Pinned by test_remote_uses_does_not_count_as_a_canary.
    """
    doc = yaml.safe_load(caller_yaml)
    out = {}
    for name, job in ((doc or {}).get("jobs") or {}).items():
        if (job or {}).get("uses") != callee_path:
            continue
        out[name] = set(((job or {}).get("with")) or {})
    return out


RANK = {"none": 0, "read": 1, "write": 2}


def needed_permissions(workflow_yaml):
    """Union of a reusable's job-level permissions -- what a caller MUST grant.

    A called workflow is CALLER-CAPPED: it can never receive more than the
    calling job grants, and GitHub validates that at workflow STARTUP. An
    under-grant does not warn or skip; the whole run dies with ZERO jobs, which
    is an absence, not a red. That invariant used to be prose in canaries.yml
    with nothing checking it, so a callee adding a permission would have taken
    every interface check down at once, silently.
    """
    doc = yaml.safe_load(workflow_yaml)
    out = {}
    for job in ((doc or {}).get("jobs") or {}).values():
        perms = (job or {}).get("permissions") or {}
        if not isinstance(perms, dict):
            continue
        for k, v in perms.items():
            if RANK.get(str(v), 0) > RANK.get(str(out.get(k, "none")), 0):
                out[k] = v
    return out


def granted(caller_yaml, callee_path):
    """job name -> permissions dict that job grants to callee_path."""
    doc = yaml.safe_load(caller_yaml)
    out = {}
    for name, job in ((doc or {}).get("jobs") or {}).items():
        if (job or {}).get("uses") != callee_path:
            continue
        out[name] = (job or {}).get("permissions") or {}
    return out


def under_granted(need, grants):
    """Messages for canary jobs granting less than their callee needs."""
    msgs = []
    for job, have in sorted(grants.items()):
        short = [
            f"{k}:{v}" for k, v in sorted(need.items())
            if RANK.get(str(have.get(k, "none")), 0) < RANK.get(str(v), 0)
        ]
        if short:
            msgs.append(
                f"canary job '{job}' under-grants {short} - a caller-capped "
                "callee dies at STARTUP with zero jobs, not a red"
            )
    return msgs


def drifted(all_inputs, required, jobs):
    """Return DRIFT messages for one reusable; empty list means covered.

    `jobs` is {canary job label -> set of input names it passes}. Extracted from
    main() so the RULE has unit rows of its own: it was previously exercised only
    by forcing a red against the live tree, which is a real arm but leaves a
    refactor free to change the rule with every unit test still green.
    """
    if not jobs:
        return ["no canary calls it - its interface is unchecked"]
    msgs = []
    # Exact-set membership, never "the biggest one": two canaries passing
    # different sets of equal size would let max(key=len) decide a gate on a
    # tie-break. `closest` only makes the message useful.
    if not any(v == all_inputs for v in jobs.values()):
        closest = max(jobs.values(), key=len)
        msgs.append(
            f"no MAX canary passes the full input set; closest is missing "
            f"{sorted(all_inputs - closest)}, extra {sorted(closest - all_inputs)}"
        )
    # Where MAX passes nothing it already IS the MIN, so only demand one when
    # the reusable actually declares inputs.
    if all_inputs and not any(v == required for v in jobs.values()):
        msgs.append(
            f"no MIN canary passing exactly the required set "
            f"{sorted(required) or '(none)'}"
        )
    return msgs


def main():
    if not WF.is_dir():
        print(f"NOT MEASURED - {WF} is not a directory")
        return 2
    callers = {}
    for name in CANARY_FILES:
        p = WF / name
        if not p.exists():
            print(f"NOT MEASURED - canary file {p} is missing")
            return 2
        callers[name] = p.read_text()

    reusables, drift = 0, 0
    for f in sorted(WF.glob(WORKFLOW_GLOB)):
        if f.name in CANARY_FILES:
            continue
        d = declared(f.read_text())
        if d is None:
            print(f"  {f.name}: not a reusable (no workflow_call) - skipped")
            continue
        all_i, req = d
        reusables += 1
        rel = f"./.github/workflows/{f.name}"
        jobs = {}
        for fname, src in callers.items():
            # Namespace by FILE: two canary files may legitimately reuse a job
            # name, and a bare update() would silently drop one -- shrinking the
            # measured population without saying so.
            for job, ins in passed(src, rel).items():
                jobs[f"{fname}:{job}"] = ins
        msgs = drifted(all_i, req, jobs)
        need = needed_permissions(f.read_text())
        grants = {}
        for fname, src in callers.items():
            for job, perms in granted(src, rel).items():
                grants[f"{fname}:{job}"] = perms
        msgs += under_granted(need, grants)
        if msgs:
            drift += len(msgs)
            for m in msgs:
                print(f"  DRIFT {f.name}: {m}")
        else:
            print(f"  ok {f.name}: MAX passes all {len(all_i)} input(s)")

    # PRINT THE DENOMINATOR: "0 drift" over an unstated population is not a pass.
    print(f"-- {reusables} reusable(s) checked, {drift} drifted --")
    if reusables == 0:
        print("NOT MEASURED - zero reusables found; an empty population is not a pass")
        return 2
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
