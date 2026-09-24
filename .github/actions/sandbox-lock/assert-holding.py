#!/usr/bin/env python3
"""Selftest helper: exit 0 iff <holder> currently holds the lock as <role>."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox_lock as sl  # noqa: E402

role, holder = sys.argv[1], sys.argv[2]
_, state = sl.Store(os.environ["SANDBOX_LOCK_TABLE"], sl.DEFAULT_KEY).read()
state = sl.prune(state, int(time.time()))
held = holder in state["readers"] if role == "reader" else (state["writer"] or {}).get("holder") == holder
print(f"{'HOLDING' if held else 'NOT HOLDING'} {role} {holder}: {state}")
sys.exit(0 if held else 1)
