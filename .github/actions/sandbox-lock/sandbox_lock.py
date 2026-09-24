#!/usr/bin/env python3
"""Cross-repo reader/writer lease lock for the shared sandbox AWS account.

GitHub concurrency groups are repository-scoped, so a group name shared by several
repos serialises nothing. This lock lives in ONE DynamoDB item that every lane can
reach. Every change is read -> compute -> conditional PutItem on the item's version
(compare-and-set): two runners racing on the same version cannot both win.

Terratest takes it as a shared READER; the sandbox nuke as an exclusive WRITER. A
waiting writer records an INTENT that stops new readers, so a stream of PR tests
cannot starve the nuke. Every entry carries a lease expiry; a holder that dies stops
renewing and its lease lapses on its own.

Verdicts (exit codes), and they are deliberately distinct:
  0  ACQUIRED / RELEASED
  1  TIMED OUT     - waited max-wait; the message names every holder in the way
  2  LOCK UNREADABLE - the store could not be read or written. NOT a held lock.
  3  LEASE LOST    - this holder's lease lapsed while it believed it held the lock
 64  USAGE         - bad arguments; nothing was measured
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time

DEFAULT_TABLE = "brd-sndbx-ue1-core-sandbox-lock"
DEFAULT_KEY = "sandbox"
EXIT_OK, EXIT_TIMEOUT, EXIT_UNREADABLE, EXIT_LOST, EXIT_USAGE = 0, 1, 2, 3, 64
ROLES = ("reader", "writer")


class Unreadable(Exception):
    """The lock store could not be read or written."""


class Conflict(Exception):
    """A conditional write lost a compare-and-set race."""


class Contended(Exception):
    """Every compare-and-set attempt in one transaction lost a race. The store is
    healthy and busy; callers treat this as "still waiting", never as UNREADABLE."""


def empty_state():
    return {"readers": {}, "writer": None, "intent": None}


def _live(entry, now):
    return entry is not None and entry["exp"] > now


def prune(state, now):
    # COPY the nested entries. Carrying writer/intent through by REFERENCE let renew()'s
    # `s["writer"]["exp"] = ...` mutate the dict its CALLER passed in, while the docstrings
    # advertise these as pure. Harmless in production today only because every real caller
    # gets state from store.read() (fresh json.loads) and FakeStore.read() deep-copies --
    # i.e. nothing could catch it. A latent trap for the next reuse of prune().
    w, i = state.get("writer"), state.get("intent")
    return {
        "readers": {h: e for h, e in state.get("readers", {}).items() if e > now},
        "writer": dict(w) if _live(w, now) else None,
        "intent": dict(i) if _live(i, now) else None,
    }


def _valid_entry(entry):
    return entry is None or (isinstance(entry, dict) and isinstance(entry.get("holder"), str)
                             and isinstance(entry.get("exp"), int))


def valid_state(state):
    return (isinstance(state, dict)
            and isinstance(state.get("readers"), dict)
            and all(isinstance(h, str) and isinstance(e, int) for h, e in state["readers"].items())
            and _valid_entry(state.get("writer"))
            and _valid_entry(state.get("intent")))


def _check_role(role):
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")


def try_acquire(state, holder, role, now, lease):
    """Return (state_to_write or None, acquired, blockers)."""
    _check_role(role)
    s = prune(state, now)
    if role == "reader":
        blockers = []
        if s["writer"]:
            blockers.append(f"writer {s['writer']['holder']}")
        if s["intent"]:
            blockers.append(f"waiting writer {s['intent']['holder']}")
        if blockers:
            return None, False, blockers
        s["readers"][holder] = now + lease
        return s, True, []

    blockers = []
    if s["writer"] and s["writer"]["holder"] != holder:
        blockers.append(f"writer {s['writer']['holder']}")
    if s["intent"] and s["intent"]["holder"] != holder:
        blockers.append(f"waiting writer {s['intent']['holder']}")
    blockers.extend(f"reader {h}" for h in sorted(s["readers"]))
    if blockers:
        if s["intent"] is None or s["intent"]["holder"] == holder:
            s["intent"] = {"holder": holder, "exp": now + lease}
            return s, False, blockers
        return None, False, blockers
    s["writer"] = {"holder": holder, "exp": now + lease}
    s["intent"] = None
    return s, True, []


def renew(state, holder, role, now, lease):
    """Return (state_to_write or None, still_held)."""
    _check_role(role)
    s = prune(state, now)
    if role == "reader":
        if holder not in s["readers"]:
            return None, False
        s["readers"][holder] = now + lease
        return s, True
    if s["writer"] and s["writer"]["holder"] == holder:
        s["writer"]["exp"] = now + lease
        return s, True
    return None, False


def release(state, holder, role, now):
    """Return (state_to_write, was_held). Always writes, so expired entries get pruned."""
    _check_role(role)
    s = prune(state, now)
    held = False
    if role == "reader" and holder in s["readers"]:
        del s["readers"][holder]
        held = True
    if role == "writer" and s["writer"] and s["writer"]["holder"] == holder:
        s["writer"] = None
        held = True
    if s["intent"] and s["intent"]["holder"] == holder:
        s["intent"] = None
    return s, held


def _tail(text, limit=300):
    text = (text or "").strip()
    return text[-limit:] if text else "(no stderr)"


class Store:
    """The lock item, through the aws CLI. `run` is injectable for tests."""

    def __init__(self, table, key, run=None):
        self.table, self.key, self.run = table, key, run or subprocess.run

    def _aws(self, *args):
        return self.run(["aws", "dynamodb", *args, "--output", "json"], capture_output=True, text=True)

    def read(self):
        p = self._aws("get-item", "--table-name", self.table, "--consistent-read",
                      "--key", json.dumps({"pk": {"S": self.key}}))
        if p.returncode != 0:
            raise Unreadable(_tail(p.stderr))
        try:
            item = json.loads(p.stdout or "{}").get("Item")
            if item is None:
                return None, empty_state()
            version, state = int(item["lock_version"]["N"]), json.loads(item["lock_state"]["S"])
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise Unreadable(f"malformed lock item: {e!r}") from e
        if not valid_state(state):
            raise Unreadable(f"malformed lock state: {state!r}")
        return version, state

    def write(self, version, state):
        new_version = 1 if version is None else version + 1
        item = {
            "pk": {"S": self.key},
            "lock_version": {"N": str(new_version)},
            "lock_state": {"S": json.dumps(state, sort_keys=True)},
        }
        args = ["put-item", "--table-name", self.table, "--item", json.dumps(item)]
        if version is None:
            args += ["--condition-expression", "attribute_not_exists(pk)"]
        else:
            args += ["--condition-expression", "#v = :v",
                     "--expression-attribute-names", json.dumps({"#v": "lock_version"}),
                     "--expression-attribute-values", json.dumps({":v": {"N": str(version)}})]
        p = self._aws(*args)
        if p.returncode == 0:
            return
        if "ConditionalCheckFailedException" in (p.stderr or ""):
            raise Conflict()
        raise Unreadable(_tail(p.stderr))


def transact(store, fn, attempts=8, sleep=time.sleep):
    """fn(state) -> (state_to_write or None, result). Retries lost CAS races."""
    for attempt in range(attempts):
        version, state = store.read()
        new_state, result = fn(state)
        if new_state is None:
            return result
        try:
            store.write(version, new_state)
            return result
        except Conflict:
            sleep(random.uniform(0.05, 0.5) * (attempt + 1))
    raise Contended(f"lost the compare-and-set race {attempts} times running")


def _say(msg):
    print(msg, flush=True)


def _holders(state, exclude):
    out = [f"reader {h}" for h in sorted(state["readers"]) if h != exclude]
    if state["writer"] and state["writer"]["holder"] != exclude:
        out.append(f"writer {state['writer']['holder']}")
    return out


def cmd_acquire(store, holder, role, lease, max_wait, poll, clock=time.time, sleep=time.sleep):
    deadline = clock() + max_wait
    while True:
        now = int(clock())

        def step(s, now=now):
            new, ok, blockers = try_acquire(s, holder, role, now, lease)
            return new, (ok, blockers, _holders(prune(s, now), holder))

        try:
            ok, blockers, others = transact(store, step, sleep=sleep)
        except Contended as e:
            ok, blockers, others = False, [f"(contended: {e})"], []
        if ok:
            _say(f"sandbox-lock: ACQUIRED {role} {holder} (lease {lease}s); "
                 f"also holding: {', '.join(others) or 'nobody'}")
            return EXIT_OK
        if clock() >= deadline:
            try:
                transact(store, lambda s: release(s, holder, role, int(clock())), sleep=sleep)
            except (Unreadable, Contended) as e:
                _say(f"sandbox-lock: could not clear own intent ({e}); it lapses within {lease}s")
            _say(f"::error::sandbox-lock: TIMED OUT after {max_wait}s waiting as {role}; "
                 f"held by: {', '.join(blockers)}")
            return EXIT_TIMEOUT
        _say(f"sandbox-lock: WAITING as {role} {holder}; held by: {', '.join(blockers)}")
        sleep(poll + random.uniform(0, poll / 4))


def _mark_lost(lost_file, reason):
    if lost_file:
        with open(lost_file, "w", encoding="utf-8") as f:
            f.write(reason + "\n")
    _say(f"sandbox-lock: LEASE LOST - {reason}")


def cmd_renew_loop(store, holder, role, lease, interval, lost_file,
                   clock=time.time, sleep=time.sleep, max_iterations=None):
    last_ok = clock()
    n = 0
    while max_iterations is None or n < max_iterations:
        n += 1
        sleep(interval)
        now = int(clock())
        try:
            held = transact(store, lambda s, now=now: renew(s, holder, role, now, lease), sleep=sleep)
        except (Unreadable, Contended) as e:
            _say(f"sandbox-lock: RENEW FAILED - {e}")
            if clock() - last_ok >= lease:
                _mark_lost(lost_file, f"no successful renewal for {lease}s (last error: {e})")
                return EXIT_LOST
            continue
        if not held:
            _mark_lost(lost_file, "holder entry was gone at renewal")
            return EXIT_LOST
        last_ok = clock()
    return EXIT_OK


def cmd_release(store, holder, role, lost_file, expect_held, clock=time.time, sleep=time.sleep):
    lost = None
    if lost_file and os.path.exists(lost_file):
        with open(lost_file, encoding="utf-8") as f:
            lost = f.read().strip()
    try:
        held = transact(store, lambda s: release(s, holder, role, int(clock())), sleep=sleep)
    except (Unreadable, Contended) as e:
        _say(f"::warning::sandbox-lock: RELEASE FAILED - {e}; the lease lapses on its own")
        held = None
    if lost:
        _say(f"::error::sandbox-lock: LEASE LOST during this job - {lost}. "
             "The sandbox was NOT protected for part of this run.")
        return EXIT_LOST
    if held is False and expect_held:
        _say(f"::error::sandbox-lock: LEASE LOST - {holder} was no longer holding at release. "
             "The sandbox was NOT protected for part of this run.")
        return EXIT_LOST
    if held:
        _say(f"sandbox-lock: RELEASED {role} {holder}")
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"::error::sandbox-lock: USAGE - {message}; nothing was measured", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def main(argv=None):
    p = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("acquire", "renew-loop", "release"))
    p.add_argument("--role", required=True, choices=ROLES)
    p.add_argument("--holder", required=True)
    p.add_argument("--table", default=DEFAULT_TABLE)
    p.add_argument("--key", default=DEFAULT_KEY)
    p.add_argument("--lease", type=int, default=600)
    p.add_argument("--max-wait", type=int, default=1200)
    p.add_argument("--poll", type=int, default=20)
    p.add_argument("--interval", type=int, default=120)
    p.add_argument("--lost-file")
    p.add_argument("--expect-held", action="store_true")
    a = p.parse_args(argv)
    store = Store(a.table, a.key)
    try:
        if a.command == "acquire":
            return cmd_acquire(store, a.holder, a.role, a.lease, a.max_wait, a.poll)
        if a.command == "renew-loop":
            return cmd_renew_loop(store, a.holder, a.role, a.lease, a.interval, a.lost_file)
        return cmd_release(store, a.holder, a.role, a.lost_file, a.expect_held)
    except Unreadable as e:
        _say(f"::error::sandbox-lock: LOCK UNREADABLE - {e}. This is NOT a held lock; "
             "the lock store itself could not be used.")
        return EXIT_UNREADABLE
    except Exception as e:  # noqa: BLE001 - an unexpected crash must not exit 1 (= TIMED OUT)
        _say(f"::error::sandbox-lock: LOCK UNREADABLE - internal error {e!r}. This is NOT a held lock.")
        return EXIT_UNREADABLE


if __name__ == "__main__":
    sys.exit(main())
