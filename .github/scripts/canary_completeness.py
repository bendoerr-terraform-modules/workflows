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
    """job name -> set of input names that job passes to callee_path."""
    doc = yaml.safe_load(caller_yaml)
    out = {}
    for name, job in ((doc or {}).get("jobs") or {}).items():
        if (job or {}).get("uses") != callee_path:
            continue
        out[name] = set(((job or {}).get("with")) or {})
    return out


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
    for f in sorted(WF.glob("*.yml")):
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
        if not jobs:
            drift += 1
            print(f"  DRIFT {f.name}: no canary calls it - its interface is unchecked")
            continue
        best = max(jobs.values(), key=len)
        if best != all_i:
            drift += 1
            print(f"  DRIFT {f.name}: no MAX canary passes the full input set; "
                  f"missing {sorted(all_i - best)}, extra {sorted(best - all_i)}")
        else:
            print(f"  ok {f.name}: MAX passes all {len(all_i)} input(s)")
        if all_i and not any(v == req for v in jobs.values()):
            drift += 1
            print(f"  DRIFT {f.name}: no MIN canary passing exactly the required "
                  f"set {sorted(req) or '(none)'}")

    # PRINT THE DENOMINATOR: "0 drift" over an unstated population is not a pass.
    print(f"-- {reusables} reusable(s) checked, {drift} drifted --")
    if reusables == 0:
        print("NOT MEASURED - zero reusables found; an empty population is not a pass")
        return 2
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
